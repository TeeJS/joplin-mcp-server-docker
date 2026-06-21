"""Joplin MCP Server implementation."""

import logging
import os
import sys
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

# Add the src directory to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.joplin.joplin_api import JoplinAPI, JoplinNote, OrderDirection
from src.joplin.joplin_utils import (
    MarkdownContent,
    get_joplin_url_from_env,
    get_token_from_env,
)

# MCP transport binding (overridable for containerized deployments)
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0").strip() or "0.0.0.0"
MCP_PORT = int(os.environ.get("MCP_PORT", "8000").strip() or "8000")
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http").strip() or "streamable-http"

# Initialize FastMCP server. host/port are read by the streamable-http and sse transports.
mcp = FastMCP("joplin", host=MCP_HOST, port=MCP_PORT)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Joplin API client
try:
    api = JoplinAPI(
        token=get_token_from_env(),
        base_url=get_joplin_url_from_env(),
    )
    logger.info("Successfully initialized Joplin API client")
except Exception as e:
    logger.error(f"Failed to initialize Joplin API client: {e}")
    api = None

# Input Models
class SearchNotesInput(BaseModel):
    """Input parameters for searching notes."""
    query: str
    limit: Optional[int] = 100

class CreateNoteInput(BaseModel):
    """Input parameters for creating a note."""
    title: str
    body: Optional[str] = None
    parent_id: Optional[str] = None
    is_todo: Optional[bool] = False

class UpdateNoteInput(BaseModel):
    """Input parameters for updating a note."""
    note_id: str
    title: Optional[str] = None
    body: Optional[str] = None
    parent_id: Optional[str] = None
    is_todo: Optional[bool] = None

class ImportMarkdownInput(BaseModel):
    """Input parameters for importing markdown files."""
    file_path: str

class TagNoteInput(BaseModel):
    """Input parameters for attaching/detaching a tag on a note."""
    note_id: str
    tag_title: str

# MCP Tools
@mcp.tool()
async def search_notes(args: SearchNotesInput) -> Dict[str, Any]:
    """Search for notes in Joplin.
    
    Args:
        args: Search parameters
            query: Search query string
            limit: Maximum number of results (default: 100)
    
    Returns:
        Dictionary containing search results
    """
    if not api:
        return {"error": "Joplin API client not initialized"}
    
    try:
        results = api.search_notes(query=args.query, limit=args.limit)
        return {
            "status": "success",
            "total": len(results.items),
            "has_more": results.has_more,
            "notes": [
                {
                    "id": note.id,
                    "title": note.title,
                    "body": note.body,
                    "created_time": note.created_time.isoformat() if note.created_time else None,
                    "updated_time": note.updated_time.isoformat() if note.updated_time else None,
                    "is_todo": note.is_todo
                }
                for note in results.items
            ]
        }
    except Exception as e:
        logger.error(f"Error searching notes: {e}")
        return {"error": str(e)}

@mcp.tool()
async def get_note(note_id: str) -> Dict[str, Any]:
    """Get a specific note by ID.
    
    Args:
        note_id: ID of the note to retrieve
    
    Returns:
        Dictionary containing the note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}
    
    try:
        note = api.get_note(note_id)
        return {
            "status": "success",
            "note": {
                "id": note.id,
                "title": note.title,
                "body": note.body,
                "created_time": note.created_time.isoformat() if note.created_time else None,
                "updated_time": note.updated_time.isoformat() if note.updated_time else None,
                "is_todo": note.is_todo
            }
        }
    except Exception as e:
        logger.error(f"Error getting note: {e}")
        return {"error": str(e)}

@mcp.tool()
async def create_note(args: CreateNoteInput) -> Dict[str, Any]:
    """Create a new note in Joplin.
    
    Args:
        args: Note creation parameters
            title: Note title
            body: Note content in Markdown (optional)
            parent_id: ID of parent folder (optional)
            is_todo: Whether this is a todo item (optional)
    
    Returns:
        Dictionary containing the created note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}
    
    try:
        note = api.create_note(
            title=args.title,
            body=args.body,
            parent_id=args.parent_id,
            is_todo=args.is_todo
        )
        return {
            "status": "success",
            "note": {
                "id": note.id,
                "title": note.title,
                "body": note.body,
                "created_time": note.created_time.isoformat() if note.created_time else None,
                "updated_time": note.updated_time.isoformat() if note.updated_time else None,
                "is_todo": note.is_todo
            }
        }
    except Exception as e:
        logger.error(f"Error creating note: {e}")
        return {"error": str(e)}

@mcp.tool()
async def update_note(args: UpdateNoteInput) -> Dict[str, Any]:
    """Update an existing note in Joplin.
    
    Args:
        args: Note update parameters
            note_id: ID of note to update
            title: New title (optional)
            body: New content (optional)
            parent_id: New parent folder ID (optional)
            is_todo: New todo status (optional)
    
    Returns:
        Dictionary containing the updated note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}
    
    try:
        note = api.update_note(
            note_id=args.note_id,
            title=args.title,
            body=args.body,
            parent_id=args.parent_id,
            is_todo=args.is_todo
        )
        return {
            "status": "success",
            "note": {
                "id": note.id,
                "title": note.title,
                "body": note.body,
                "created_time": note.created_time.isoformat() if note.created_time else None,
                "updated_time": note.updated_time.isoformat() if note.updated_time else None,
                "is_todo": note.is_todo
            }
        }
    except Exception as e:
        logger.error(f"Error updating note: {e}")
        return {"error": str(e)}

@mcp.tool()
async def delete_note(note_id: str, permanent: bool = False) -> Dict[str, Any]:
    """Delete a note from Joplin.
    
    Args:
        note_id: ID of note to delete
        permanent: If True, permanently delete the note
    
    Returns:
        Dictionary containing the operation status
    """
    if not api:
        return {"error": "Joplin API client not initialized"}
    
    try:
        api.delete_note(note_id, permanent=permanent)
        return {
            "status": "success",
            "message": f"Note {note_id} {'permanently ' if permanent else ''}deleted"
        }
    except Exception as e:
        logger.error(f"Error deleting note: {e}")
        return {"error": str(e)}

@mcp.tool()
async def import_markdown(args: ImportMarkdownInput) -> Dict[str, Any]:
    """Import a markdown file as a new note.
    
    Args:
        args: Import parameters
            file_path: Path to the markdown file
    
    Returns:
        Dictionary containing the created note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}
    
    try:
        file_path = Path(args.file_path)
        md_content = MarkdownContent.from_file(file_path)
        
        note = api.create_note(
            title=md_content.title,
            body=md_content.content
        )
        
        return {
            "status": "success",
            "note": {
                "id": note.id,
                "title": note.title,
                "body": note.body,
                "created_time": note.created_time.isoformat() if note.created_time else None,
                "updated_time": note.updated_time.isoformat() if note.updated_time else None,
                "is_todo": note.is_todo
            },
            "imported_from": str(file_path)
        }
    except Exception as e:
        logger.error(f"Error importing markdown: {e}")
        return {"error": str(e)}

@mcp.tool()
async def list_tags() -> Dict[str, Any]:
    """List every tag that exists in Joplin.

    Use this to discover the available tag vocabulary before tagging a note.
    New tags must be created by the user in Joplin Desktop; this server cannot
    create them.

    Returns:
        Dictionary with the full tag list as {id, title} entries.
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        tags = api.list_tags()
        return {"status": "success", "total": len(tags), "tags": tags}
    except Exception as e:
        logger.error(f"Error listing tags: {e}")
        return {"error": str(e)}


@mcp.tool()
async def get_note_tags(note_id: str) -> Dict[str, Any]:
    """List the tags currently attached to a specific note.

    Args:
        note_id: ID of the note.

    Returns:
        Dictionary containing the note's tags as {id, title} entries.
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        tags = api.get_note_tags(note_id)
        return {"status": "success", "note_id": note_id, "tags": tags}
    except Exception as e:
        logger.error(f"Error getting tags for note {note_id}: {e}")
        return {"error": str(e)}


@mcp.tool()
async def tag_note(args: TagNoteInput) -> Dict[str, Any]:
    """Attach an EXISTING tag to a note.

    The tag must already exist in Joplin; this server will not create new tags.
    If the tag does not exist, the call fails with a clear error so the model
    can pick a different existing tag (use list_tags to see what's available).

    Args:
        args: note_id and tag_title (case-insensitive match against existing tags).

    Returns:
        Dictionary describing the result, including the resolved tag id.
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        tag = api.find_tag_by_title(args.tag_title)
        if tag is None:
            return {
                "error": (
                    f"Tag '{args.tag_title}' does not exist. Tag creation is "
                    "disabled on this server. Call list_tags to see available "
                    "tags, or ask the user to create the tag in Joplin Desktop."
                )
            }

        api.add_existing_tag_to_note(tag_id=tag["id"], note_id=args.note_id)
        return {
            "status": "success",
            "note_id": args.note_id,
            "tag": tag,
        }
    except Exception as e:
        logger.error(f"Error tagging note {args.note_id}: {e}")
        return {"error": str(e)}


@mcp.tool()
async def untag_note(args: TagNoteInput) -> Dict[str, Any]:
    """Remove a tag from a note. The tag itself is not deleted from Joplin.

    Args:
        args: note_id and tag_title (case-insensitive match against existing tags).

    Returns:
        Dictionary describing the result.
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        tag = api.find_tag_by_title(args.tag_title)
        if tag is None:
            return {"error": f"Tag '{args.tag_title}' does not exist."}

        api.remove_tag_from_note(tag_id=tag["id"], note_id=args.note_id)
        return {
            "status": "success",
            "note_id": args.note_id,
            "tag": tag,
        }
    except Exception as e:
        logger.error(f"Error untagging note {args.note_id}: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    logger.info(
        "Starting Joplin MCP Server (transport=%s, host=%s, port=%s)",
        MCP_TRANSPORT, MCP_HOST, MCP_PORT,
    )
    mcp.run(transport=MCP_TRANSPORT)
