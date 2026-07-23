"""Model Context Protocol (MCP) Server for Meetings Bot.

This server exposes tools to search and retrieve meeting summaries/details from
the ChromaDB database. It uses standard I/O (stdio) transport, meaning it runs
as a separate process on-demand, causing zero overhead to the live bot/FastAPI server.
"""

import os
import sys
import logging
from typing import Optional

# Setup logging to sys.stderr (MCP uses stdout for protocol communications)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("meetings_bot.mcp_server")

# Add the project root to Python path so we can resolve project packages
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(project_root)

import chromadb
from mcp.server.fastmcp import FastMCP
from src.teams_agent.config import Config

# Initialize FastMCP Server
mcp = FastMCP("MeetingsBotMemory")

def get_chroma_collection():
    """Retrieve/create the ChromaDB meeting summaries collection."""
    # Resolve relative db paths to absolute path if needed
    db_path = Config.CHROMADB_PATH
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(os.path.join(project_root, db_path))
    
    logger.info(f"Connecting to ChromaDB at: {db_path}")
    client = chromadb.PersistentClient(path=db_path)
    return client.get_or_create_collection(
        name="meeting_summaries",
        metadata={"hnsw:space": "cosine"},
    )

@mcp.tool()
def list_meetings() -> str:
    """List all meetings stored in the database with their IDs, dates, and URLs.
    
    Use this to get an overview of all meetings that have been recorded so far.
    """
    try:
        collection = get_chroma_collection()
        results = collection.get(include=["metadatas", "documents"])
        
        if not results or not results["ids"]:
            return "No meetings recorded in the database yet."
        
        output = ["### Recorded Meetings in Database:"]
        for doc_id, meta, doc in zip(results["ids"], results["metadatas"], results["documents"]):
            date = meta.get("date", "Unknown Date")
            url = meta.get("meeting_url", "No URL Provided")
            # Truncate summary for listing
            summary_preview = doc[:200] + "..." if len(doc) > 200 else doc
            
            output.append(
                f"- **ID**: `{doc_id}`\n"
                f"  **Date**: {date}\n"
                f"  **Meeting URL**: {url}\n"
                f"  **Preview**: {summary_preview}\n"
            )
        return "\n".join(output)
    except Exception as e:
        logger.exception("Error listing meetings")
        return f"Error listing meetings: {str(e)}"

@mcp.tool()
def search_meetings(query: str, limit: int = 3) -> str:
    """Semantic search over past meeting summaries in ChromaDB.
    
    Use this to find what was discussed in past meetings about specific keywords or topics.
    """
    try:
        collection = get_chroma_collection()
        results = collection.query(
            query_texts=[query],
            n_results=limit,
        )
        
        if not results or not results["documents"] or not results["documents"][0]:
            return f"No matching meetings found for query: '{query}'"
        
        output = [f"### Search Results for: '{query}'\n"]
        for doc, meta, doc_id in zip(results["documents"][0], results["metadatas"][0], results["ids"]):
            date = meta.get("date", "Unknown Date")
            url = meta.get("meeting_url", "No URL")
            output.append(
                f"#### Meeting ID: `{doc_id}`\n"
                f"- **Date**: {date}\n"
                f"- **URL**: {url}\n"
                f"- **Summary**:\n{doc}\n"
                f"{'-' * 40}"
            )
        return "\n\n".join(output)
    except Exception as e:
        logger.exception("Error searching meetings")
        return f"Error searching meetings: {str(e)}"

@mcp.tool()
def get_meeting_details(meeting_id: str) -> str:
    """Retrieve full details of a specific meeting, including the full summary and a snippet of the transcript.
    
    Use this when you have a meeting ID and want to see the details of that specific session.
    """
    try:
        collection = get_chroma_collection()
        results = collection.get(ids=[meeting_id], include=["metadatas", "documents"])
        
        if not results or not results["ids"]:
            return f"Meeting with ID '{meeting_id}' not found."
        
        doc = results["documents"][0]
        meta = results["metadatas"][0]
        date = meta.get("date", "Unknown Date")
        url = meta.get("meeting_url", "No URL")
        transcript_snippet = meta.get("transcript", "No transcript snippet available.")
        
        return (
            f"### Meeting Details: `{meeting_id}`\n"
            f"- **Date**: {date}\n"
            f"- **URL**: {url}\n\n"
            f"#### Full Summary:\n{doc}\n\n"
            f"#### Transcript Snippet (Up to 5k chars):\n```\n{transcript_snippet}\n```"
        )
    except Exception as e:
        logger.exception("Error getting meeting details")
        return f"Error getting meeting details: {str(e)}"

@mcp.tool()
def get_system_status() -> str:
    """Retrieve the current configuration status of the Meetings Bot.
    
    Includes details like name, active pipeline mode, and vision setting.
    """
    return (
        f"### Meetings Bot Status\n"
        f"- **Bot Name**: {Config.BOT_NAME}\n"
        f"- **Pipeline Mode**: {Config.PIPELINE_MODE}\n"
        f"- **Vision Observer**: {'Enabled' if Config.VISION_ENABLED else 'Disabled'}\n"
        f"- **Local DB Location**: `{Config.CHROMADB_PATH}`\n"
        f"- **FastAPI API URL**: `http://localhost:6789`"
    )

if __name__ == "__main__":
    logger.info("Starting MeetingsBot MCP Server (stdio mode)")
    mcp.run()
