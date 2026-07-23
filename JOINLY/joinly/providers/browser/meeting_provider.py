import asyncio
import contextlib
import io
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Self

from PIL import Image, ImageOps
from playwright.async_api import Page

from joinly.core import AudioReader, AudioWriter, VideoReader
from joinly.providers.base import BaseMeetingProvider
from joinly.providers.browser.browser_session import BrowserSession
from joinly.providers.browser.camera_feed import CameraFeed
from joinly.providers.browser.devices.pulse_server import PulseServer
from joinly.providers.browser.devices.virtual_display import VirtualDisplay
from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
from joinly.providers.browser.devices.virtual_speaker import VirtualSpeaker
from joinly.providers.browser.platforms import (
    BrowserPlatformController,
    GoogleMeetBrowserPlatformController,
    TeamsBrowserPlatformController,
    ZoomBrowserPlatformController,
)
from joinly.providers.browser.screen_share import remove_overlay, setup_content_stream
from joinly.settings import get_settings
from joinly.types import (
    ActionAnimation,
    AudioChunk,
    MeetingChatHistory,
    MeetingParticipant,
    ProviderNotSupportedError,
    UIAnimationContent,
    UIHtmlContent,
    UIUpdate,
    VideoSnapshot,
)

logger = logging.getLogger(__name__)

PLATFORMS: list[type[BrowserPlatformController]] = [
    GoogleMeetBrowserPlatformController,
    TeamsBrowserPlatformController,
    ZoomBrowserPlatformController,
]


class _SpeakerInjectedAudioReader(AudioReader):
    """Audio reader that injects audio into the virtual speaker."""

    def __init__(
        self, reader: AudioReader, get_reader: Callable[[], str | None]
    ) -> None:
        """Initialize the audio reader with the virtual speaker."""
        self._reader = reader
        self._get_reader = get_reader
        self.audio_format = reader.audio_format

    async def read(self) -> AudioChunk:
        """Read audio data and inject it into the virtual speaker."""
        chunk = await self._reader.read()
        return AudioChunk(
            data=chunk.data,
            time_ns=chunk.time_ns,
            speaker=self._get_reader(),
        )


class BrowserMeetingProvider(BaseMeetingProvider, VideoReader):
    """A meeting provider that uses a web browser to join meetings."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        reader_byte_depth: int | None = None,
        writer_byte_depth: int | None = None,
        display_size: tuple[int, int] = (1280, 720),
        snapshot_size: tuple[int, int] = (512, 288),
        vnc_server: bool = False,
        vnc_server_port: int = 5900,
        admission_timeout_seconds: int = 600,
        browser_profile_dir: str | None = None,
        camera_logo_path: str | None = None,
    ) -> None:
        """Initialize the browser meeting provider.

        Args:
            reader_byte_depth (int | None): The byte depth for the virtual speaker
                (default is None).
            writer_byte_depth (int | None): The byte depth for the virtual
                microphone (default is None).
            display_size (tuple[int, int]): The virtual display and screen share
                resolution (default is (1280, 720)).
            snapshot_size (tuple[int, int]): The size of the video snapshot
                (default is (512, 288)).
            vnc_server (bool): Whether to start a VNC server for the virtual display.
            vnc_server_port (int): The port to use for the VNC server.
            admission_timeout_seconds (int): Max time to wait for lobby admission.
            browser_profile_dir (str | None): Persistent Chromium profile directory.
            camera_logo_path (str | None): Optional image file for the virtual camera
                logo. Defaults to the built-in Alex logo.
        """
        self.snapshot_size = snapshot_size
        self._display_size = display_size
        self._admission_timeout_seconds = admission_timeout_seconds
        self._env = os.environ.copy()
        self._pulse_server = PulseServer(env=self._env)
        self._virtual_display = VirtualDisplay(
            env=self._env,
            size=display_size,
            use_vnc_server=vnc_server,
            vnc_port=vnc_server_port,
        )
        self._virtual_speaker = (
            VirtualSpeaker(env=self._env)
            if not reader_byte_depth
            else VirtualSpeaker(env=self._env, byte_depth=reader_byte_depth)
        )
        self._virtual_microphone = (
            VirtualMicrophone(env=self._env)
            if not writer_byte_depth
            else VirtualMicrophone(env=self._env, byte_depth=writer_byte_depth)
        )
        self._browser_session = BrowserSession(
            env=self._env,
            profile_dir=browser_profile_dir,
        )
        self._services = [
            self._pulse_server,
            self._virtual_display,
            self._virtual_speaker,
            self._virtual_microphone,
            self._browser_session,
        ]

        self._page: Page | None = None
        self._content_page: Page | None = None
        self._is_sharing: bool = False
        self._platform_controller: BrowserPlatformController | None = None
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()

        self._camera_feed = CameraFeed(
            self._virtual_microphone,
            logo_path=camera_logo_path,
        )
        self._speaker_injected_virtual_speaker = _SpeakerInjectedAudioReader(
            self._virtual_speaker,
            lambda: (
                self._platform_controller.active_speaker
                if self._platform_controller
                else None
            ),
        )

    @property
    def audio_reader(self) -> AudioReader:
        """Get the audio reader."""
        return self._speaker_injected_virtual_speaker

    @property
    def audio_writer(self) -> AudioWriter:
        """Get the audio writer."""
        return self._camera_feed.audio_writer

    @property
    def video_reader(self) -> VideoReader:
        """Get the video reader."""
        return self

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        try:
            for service in self._services:
                await self._stack.enter_async_context(service)

        except Exception:
            await self._stack.aclose()
            raise

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Exit the context."""
        try:
            if self._page is not None and not self._page.is_closed():
                await self.leave()
        finally:
            await self._stack.aclose()

    @asynccontextmanager
    async def _action_guard(
        self, action: str
    ) -> AsyncIterator[tuple[Page, BrowserPlatformController]]:
        """Context manager to guard actions with a lock and error handling.

        Args:
            action: The action being guarded, for logging (e.g., "join", "leave", etc.).

        Yields:
            A tuple containing the current Page and the platform-specific controller.
        """
        if (
            self._page is None
            or self._page.is_closed()
            or self._platform_controller is None
        ):
            msg = f"Failed to perform '{action}'. Currently not in a meeting."
            logger.error(msg)
            raise RuntimeError(msg)

        async with self._lock:
            try:
                yield self._page, self._platform_controller
            except Exception as e:
                msg = f"Failed to perform '{action}'."
                logger.exception(msg)
                if isinstance(e, (ProviderNotSupportedError, ValueError)):
                    raise
                raise RuntimeError(msg) from None
            else:
                logger.info("Successfully performed '%s'.", action)

    async def _get_platform_controller(self, url: str) -> BrowserPlatformController:
        """Get the platform-specific meeting controller based on the URL.

        Args:
            url: The URL of the meeting.

        Returns:
            The platform-specific meeting controller.

        Raises:
            RuntimeError: If no matching platform controller is found for the URL.
        """
        for platform_controller_type in PLATFORMS:
            if platform_controller_type.url_pattern.match(url):
                if platform_controller_type is TeamsBrowserPlatformController:
                    return platform_controller_type(
                        admission_timeout_seconds=self._admission_timeout_seconds
                    )
                return platform_controller_type()

        msg = (
            f"No supported platform found for URL: {url}. "
            "Supported platforms: "
            f"{
                ', '.join(
                    pc.__name__.removesuffix('BrowserPlatformController')
                    for pc in PLATFORMS
                )
            }."
        )
        raise RuntimeError(msg)

    async def _cleanup_content_page(self) -> None:
        """Close the content page if it exists and reset sharing state."""
        if self._content_page and not self._content_page.is_closed():
            await self._content_page.close()
        self._content_page = None
        self._is_sharing = False

    async def join(
        self,
        url: str | None = None,
        name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        """Join a meeting.

        Args:
            url: The URL of the meeting to join.
            name: The name of the participant. If None, uses the default name from
                settings.
            passcode: The password or passcode for the meeting (if required).
        """
        if not url:
            msg = "Meeting URL is required to join a meeting."
            logger.error(msg)
            raise ValueError(msg)

        if self._page is not None and not self._page.is_closed():
            msg = "Meeting already joined. Leave the meeting before joining a new one."
            logger.error(msg)
            raise RuntimeError(msg)

        self._page = await self._browser_session.get_page()
        await self._camera_feed.install(self._page)

        # Install WebRTC injection patches via add_init_script so they run BEFORE
        # Teams loads any JavaScript. Three interception points cover all code paths
        # Teams may use to add the screen-share video track to the peer connection:
        #   1. getDisplayMedia  – intercepted and returns our canvas stream directly
        #   2. addTrack         – swaps any video track with canvas stream (Teams SFU path)
        #   3. replaceTrack     – swaps via sender (Teams v2 / presenter path)
        #   4. addTransceiver   – swaps when Teams adds a new transceiver for screen share
        # window.__joinlyStream is set by Python after canvas setup so all frames
        # can access the stream without a DOM lookup (avoids cross-frame issues).
        await self._page.add_init_script("""
            (function() {
                const topWin = (function() {
                    try { return window.top || window; } catch(e) { return window; }
                })();

                function _getJoinlyTrack() {
                    const s = topWin.__joinlyStream;
                    if (s) {
                        const t = s.getVideoTracks()[0];
                        if (t) return t;
                    }
                    // Fallback: grab directly from canvas element
                    try {
                        const c = topWin.document && topWin.document.getElementById('__scOverlay');
                        if (c && typeof c.captureStream === 'function') {
                            return c.captureStream(30).getVideoTracks()[0] || null;
                        }
                    } catch(_) {}
                    return null;
                }

                // ── 1. getDisplayMedia patch ──────────────────────────────────────
                if (navigator.mediaDevices && !navigator.mediaDevices.__joinlyPatched) {
                    navigator.mediaDevices.__joinlyPatched = true;
                    const _origGDM = navigator.mediaDevices.getDisplayMedia
                        .bind(navigator.mediaDevices);
                    navigator.mediaDevices.getDisplayMedia = async function(_c) {
                        const constraints = _c || {};
                        constraints.audio = false;
                        constraints.selfBrowserSurface = 'include';
                        constraints.video = constraints.video || {displaySurface: 'browser'};
                        try {
                            const stream = await _origGDM(constraints);
                            topWin.__scShareOk = true;
                            console.log('[Joinly] getDisplayMedia: native capture resolved');
                            return stream;
                        } catch(e) {
                            topWin.__scShareErr = 'native-gdm: ' + String(e);
                            topWin.__scShareOk = false;
                            throw e;
                        }
                    };
                }

                // ── 2. RTCPeerConnection.addTrack patch ───────────────────────────
                if (typeof RTCPeerConnection !== 'undefined' &&
                    !RTCPeerConnection.prototype.__joinlyAddTrackPatched) {
                    RTCPeerConnection.prototype.__joinlyAddTrackPatched = true;
                    const _origAddTrack = RTCPeerConnection.prototype.addTrack;
                    RTCPeerConnection.prototype.addTrack = function(track, ...streams) {
                        if (track && track.kind === 'video' && topWin.__scShareActive) {
                            const ct = _getJoinlyTrack();
                            if (ct) {
                                topWin.__scShareOk = true;
                                console.log('[Joinly] addTrack: canvas track injected');
                                return _origAddTrack.call(this, ct, ...streams);
                            }
                        }
                        return _origAddTrack.call(this, track, ...streams);
                    };
                }

                // ── 3. RTCRtpSender.replaceTrack patch ────────────────────────────
                // Teams v2 switches to screen share via sender.replaceTrack() rather
                // than addTrack.  Intercept and substitute our canvas track.
                if (typeof RTCRtpSender !== 'undefined' &&
                    !RTCRtpSender.prototype.__joinlyReplaceTrackPatched) {
                    RTCRtpSender.prototype.__joinlyReplaceTrackPatched = true;
                    const _origReplaceTrack = RTCRtpSender.prototype.replaceTrack;
                    RTCRtpSender.prototype.replaceTrack = function(track) {
                        if (track && track.kind === 'video' && topWin.__scShareActive) {
                            const ct = _getJoinlyTrack();
                            if (ct) {
                                topWin.__scShareOk = true;
                                console.log('[Joinly] replaceTrack: canvas track injected');
                                return _origReplaceTrack.call(this, ct);
                            }
                        }
                        return _origReplaceTrack.call(this, track);
                    };
                }

                // ── 4. RTCPeerConnection.addTransceiver patch ─────────────────────
                // Some Teams builds use addTransceiver(track, …) to add the screen track.
                if (typeof RTCPeerConnection !== 'undefined' &&
                    !RTCPeerConnection.prototype.__joinlyAddTransceiverPatched) {
                    RTCPeerConnection.prototype.__joinlyAddTransceiverPatched = true;
                    const _origAddTransceiver = RTCPeerConnection.prototype.addTransceiver;
                    RTCPeerConnection.prototype.addTransceiver = function(trackOrKind, init) {
                        if (topWin.__scShareActive) {
                            const kind = typeof trackOrKind === 'string'
                                ? trackOrKind
                                : (trackOrKind && trackOrKind.kind);
                            if (kind === 'video') {
                                const ct = _getJoinlyTrack();
                                if (ct) {
                                    topWin.__scShareOk = true;
                                    console.log('[Joinly] addTransceiver: canvas track injected');
                                    return _origAddTransceiver.call(this, ct, init);
                                }
                            }
                        }
                        return _origAddTransceiver.call(this, trackOrKind, init);
                    };
                }
            })();
        """)
        try:
            self._platform_controller = await self._get_platform_controller(url)
        except RuntimeError:
            await self._page.close()
            self._page = None
            raise

        if name is None:
            name = get_settings().name

        async with self._action_guard("join") as (page, controller):
            try:
                await controller.join(page, url, name=name, passcode=passcode)
            except Exception:
                await self._page.close()
                self._page = None
                self._platform_controller = None
                raise

    async def leave(self) -> None:
        """Leave the current meeting."""
        async with self._action_guard("leave") as (page, controller):
            try:
                if self._is_sharing:
                    await self._cleanup_content_page()
                    await page.bring_to_front()
                await controller.leave(page)
            except RuntimeError:
                logger.warning(
                    "Failed to leave the meeting, forcing page close.", exc_info=True
                )
            finally:
                self._platform_controller = None
                await self._camera_feed.stop()
                await self._cleanup_content_page()
                if self._page is not None and not self._page.is_closed():
                    await self._page.close()
                self._page = None

    async def send_chat_message(self, message: str) -> None:
        """Send a chat message in the meeting.

        Args:
            message: The message to send.
        """
        async with self._action_guard("send_chat_message") as (page, controller):
            await controller.send_chat_message(page, message)

    async def get_chat_history(self) -> MeetingChatHistory:
        """Get the chat history from the meeting.

        Returns:
            MeetingChatHistory: The chat history of the meeting.
        """
        async with self._action_guard("get_chat_history") as (page, controller):
            return await controller.get_chat_history(page)

    async def open_chat_panel(self) -> None:
        """Ensure the in-meeting chat panel is open and ready."""
        async with self._action_guard("open_chat_panel") as (page, controller):
            await controller.open_chat_panel(page)

    async def get_participants(self) -> list[MeetingParticipant]:
        """Get the list of participants in the meeting.

        Returns:
            list[MeetingParticipant]: A list of participants in the meeting.
        """
        async with self._action_guard("get_participants") as (page, controller):
            if self._is_sharing and self._content_page:
                # Canvas-based share is independent of tab focus — the WebRTC
                # stream continues regardless. Briefly bring Teams to front so
                # its DOM renders participant elements correctly, then restore.
                await page.bring_to_front()
                await asyncio.sleep(0.4)
            try:
                return await controller.get_participants(page)
            finally:
                if self._is_sharing and self._content_page:
                    await self._content_page.bring_to_front()

    async def mute(self) -> None:
        """Mute yourself in the meeting."""
        async with self._action_guard("mute") as (page, controller):
            await controller.mute(page)

    async def unmute(self) -> None:
        """Unmute yourself in the meeting."""
        async with self._action_guard("unmute") as (page, controller):
            await controller.unmute(page)

    async def share_screen(self, url: str) -> None:
        """Start sharing screen in the meeting.

        Opens *url* in a separate browser tab and streams its content
        via a full-screen canvas overlay on the meeting tab.  Tab
        self-capture ensures the platform receives a real
        ``getDisplayMedia`` stream while participants see only the
        shared content.

        Args:
            url: URL to display while sharing.
        """
        if self._is_sharing:
            msg = (
                "Already sharing screen. "
                "Stop the current share before starting a new one."
            )
            raise RuntimeError(msg)

        content_page = await self._browser_session.get_page()
        # Use domcontentloaded (not "load") so we don't wait for all images/scripts.
        # 45s covers slow connections and large pages like Wikipedia.
        await content_page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Set a fixed tab title so --auto-select-tab-capture-source-by-title
        # can auto-select this tab when Teams calls getDisplayMedia.
        await content_page.evaluate("document.title = '__joinly_share__'")

        # Bring the content tab to the foreground in the virtual display.
        # Will be set BACK before share actually fires — see below.
        await content_page.bring_to_front()
        await content_page.wait_for_timeout(800)
        logger.info(
            "Content tab brought to front — Xvfb now shows shared content."
        )

        try:
            async with self._action_guard("share_screen") as (page, controller):
                # Capture browser console to see if our getDisplayMedia patch fires.
                patch_msgs: list[str] = []
                page.on("console", lambda msg: patch_msgs.append(
                    f"[browser] {msg.type}: {msg.text}"
                ))

                await setup_content_stream(page, content_page, self._display_size)

                # Native desktop capture is the primary path. Keep WebRTC track
                # substitution disabled so Teams sends Chromium's captured stream.
                await page.evaluate(
                    "() => { window.__scShareActive = false; window.__joinlyStream = null; }"
                )
                logger.info("Native getDisplayMedia path armed for screen share.")

                # Teams' share tray only renders fully when the Teams tab is the
                # active/foreground tab in Chrome.  Bring Teams to front so the
                # tray loads all its options (including "Screen, window, or tab").
                await page.bring_to_front()
                await page.wait_for_timeout(400)

                # Verify the canvas and patch are in place before clicking share.
                diag: dict = await page.evaluate("""
                    () => ({
                        patched: !!(navigator.mediaDevices.__joinlyPatched),
                        canvas: !!document.getElementById('__scOverlay'),
                    })
                """)
                logger.info("Share pre-check: %s", diag)

                # Click "Share content" → "Screen, window, or tab" while Teams
                # tab is in the foreground so the tray fully renders.
                # controller.share_screen returns AFTER clicking the screen option
                # but BEFORE Chrome's getDisplayMedia call resolves.  We then
                # immediately bring Wikipedia back to front so the Xvfb virtual
                # display shows Wikipedia when Chrome captures the screen.
                await controller.share_screen(page)
                await content_page.bring_to_front()   # ← Wikipedia to front now
                logger.info(
                    "Content page brought to front after share option click "
                    "— Xvfb capture will see Wikipedia."
                )

                # Wait for the share to actually start.  Checks are ordered from
                # most-reliable to least-reliable.
                ok = None
                for _iter in range(20):  # up to 10 seconds
                    ok = await page.evaluate("() => window.__scShareOk")
                    if ok is not None:
                        break

                    # ── 1. Canvas brightness diagnostic ──────────────────────
                    # CDP pixels alone do not prove Teams is transmitting.
                    try:
                        canvas_state: dict = await page.evaluate("""
                            () => {
                                const c = document.getElementById('__scOverlay');
                                if (!c) return {found: false};
                                const ctx = c.getContext('2d');
                                if (!ctx) return {found: true, hasContent: false};
                                const sample = ctx.getImageData(
                                    c.width/2 - 10, c.height/2 - 10, 20, 20
                                ).data;
                                const avg = Array.from(sample).reduce((a, b) => a + b, 0)
                                    / sample.length;
                                return {found: true, hasContent: avg > 30, avgBrightness: avg};
                            }
                        """)
                        if canvas_state.get("hasContent"):
                            logger.debug(
                                "Canvas overlay has content (avgBrightness=%.1f) "
                                "while waiting for native getDisplayMedia.",
                                canvas_state.get("avgBrightness", 0),
                            )
                    except Exception:  # noqa: BLE001
                        pass

                    # ── 2. Teams confirmed "stop sharing" / "you are presenting" ─
                    # These strings only appear AFTER Teams has actually started
                    # transmitting a stream — NOT when the share tray is open.
                    # "screen sharing" is intentionally excluded because it appears
                    # in the share tray itself (false positive).
                    try:
                        body_text = await page.locator("body").inner_text(timeout=500)
                        body_norm = body_text.casefold()
                        if any(
                            s in body_norm
                            for s in (
                                "stop sharing",
                                "stop presenting",
                                "you are sharing",
                                "you are presenting",
                            )
                        ):
                            logger.info("Teams confirmed sharing active (UI indicator).")
                            ok = True
                            break
                    except Exception:  # noqa: BLE001
                        pass

                    # ── 3. Browser console VideoFrame / Joinly patch log ──────
                    if any("VideoFrame" in m or "Joinly" in m for m in patch_msgs):
                        logger.info(
                            "Teams processing video frames — sharing active. "
                            "Console: %s",
                            [m for m in patch_msgs if "VideoFrame" in m or "Joinly" in m],
                        )
                        ok = True
                        break

                    # ── 4. Non-Teams frame URL (tab-capture mode) ─────────────
                    try:
                        for frame in page.frames:
                            if frame.url and "teams" not in frame.url.casefold():
                                logger.info(
                                    "Shared content frame found: %s",
                                    frame.url.split("?")[0],
                                )
                                ok = True
                                break
                        if ok:
                            break
                    except Exception:  # noqa: BLE001
                        pass

                    await page.wait_for_timeout(500)
                if not ok:
                    # Disarm the addTrack patch before raising so it doesn't
                    # intercept future camera tracks.
                    with contextlib.suppress(Exception):
                        await page.evaluate("() => { window.__scShareActive = false; }")
                    err_msg: str = await page.evaluate(
                        "() => window.__scShareErr || 'unknown error'"
                    )
                    logger.error(
                        "getDisplayMedia failed: %s. Browser console: %s",
                        err_msg,
                        patch_msgs[-10:] if patch_msgs else "(none)",
                    )
                    msg = f"Screen sharing failed to start: {err_msg}"
                    raise ProviderNotSupportedError(msg)
                # Sharing confirmed — disarm the addTrack intercept so
                # Teams' future camera addTrack calls are NOT replaced.
                with contextlib.suppress(Exception):
                    await page.evaluate("() => { window.__scShareActive = false; }")
                # Keep __joinlyStream alive — the WebRTC track is still being sent.
                # It will be cleared in stop_sharing().
                self._content_page = content_page
                content_page = None  # ownership transferred
                self._is_sharing = True
        finally:
            if content_page and not content_page.is_closed():
                await content_page.close()

    async def stop_sharing(self) -> None:
        """Stop sharing screen in the meeting."""
        if not self._is_sharing:
            return
        async with self._action_guard("stop_sharing") as (page, controller):
            try:
                # Disarm all WebRTC patches and clear the stored stream.
                with contextlib.suppress(Exception):
                    await page.evaluate(
                        "() => { window.__scShareActive = false; window.__joinlyStream = null; }"
                    )
                await page.bring_to_front()
                await controller.stop_sharing(page)
                await remove_overlay(page)
            finally:
                await self._cleanup_content_page()

    async def set_animation(self, animation: ActionAnimation | None) -> None:
        """Set an action animation on the camera feed."""
        self._camera_feed.set_effect(animation)

    async def update_ui(self, update: UIUpdate) -> None:
        """Update the UI on the camera feed."""
        if isinstance(update.content, UIAnimationContent):
            self._camera_feed.set_effect(update.content.animation)
        elif isinstance(update.content, UIHtmlContent):
            logger.warning("HTML UI content not yet supported")

    async def snapshot(self) -> VideoSnapshot:
        """Take a snapshot of the current video frame.

        Returns:
            VideoSnapshot: The snapshot of the current video frame.
        """
        if not self._page or self._page.is_closed():
            msg = "Cannot take snapshot. Not currently in a meeting."
            logger.error(msg)
            raise RuntimeError(msg)

        raw = await self._page.screenshot(type="png")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = ImageOps.crop(img, border=int(min(*img.size) * 0.1))
        img = ImageOps.fit(
            img,
            self.snapshot_size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

        buf = io.BytesIO()
        img.save(buf, format="jpeg", quality=90, optimize=True, progressive=True)

        return VideoSnapshot(data=buf.getvalue(), media_type="image/jpeg")
