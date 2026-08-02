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

def env_str(name: str, default: str = "") -> str:
    """Read an environment variable, trimmed.

    These are pasted by hand into container UIs, where a stray tab or trailing
    space is routine and produces failures that point nowhere near the mistake.
    """
    return os.environ.get(name, default).strip() or default


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable. Only an explicit true value enables."""
    raw = env_str(name, "").lower()
    if not raw:
        return default

    return raw in {"1", "true", "yes", "on"}


# MCP transport binding (overridable for containerized deployments)
MCP_HOST = env_str("MCP_HOST", "0.0.0.0")
MCP_PORT = int(env_str("MCP_PORT", "8000"))
MCP_TRANSPORT = env_str("MCP_TRANSPORT", "streamable-http")

# --- Tool exposure policy ---------------------------------------------------
#
# The read surface, as an allowlist. Anything not named here counts as a write
# tool. This is deliberately an allowlist rather than a list of write tools: a
# tool added later and left unclassified is then treated as a write, which is
# the safe direction to fail.
READ_TOOLS = frozenset({
    "search_notes",
    "get_note",
    "list_tags",
    "get_note_tags",
})

# Disables every tool outside READ_TOOLS. Server-level, so no future group
# grant can widen it back.
JOPLIN_READ_ONLY = env_bool("JOPLIN_READ_ONLY", False)

# `import_markdown` reads a caller-supplied path off the container filesystem
# and returns its contents. Unset, the tool is not registered at all; set, it is
# confined to this directory. Default-off because an unconfined version is an
# arbitrary file read — including /proc/self/environ, which holds JOPLIN_TOKEN.
JOPLIN_IMPORT_ROOT = env_str("JOPLIN_IMPORT_ROOT", "")

# Permanent deletion bypasses the Joplin trash and cannot be undone. Off unless
# explicitly enabled; a `permanent=True` call is otherwise refused rather than
# silently downgraded, so the caller is not misled about what happened.
JOPLIN_ALLOW_PERMANENT_DELETE = env_bool("JOPLIN_ALLOW_PERMANENT_DELETE", False)

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

def resolve_import_path(raw_path: str) -> Path:
    """Resolve a caller-supplied import path, confined to JOPLIN_IMPORT_ROOT.

    Resolves symlinks and `..` before comparing, so neither can be used to walk
    out of the configured root. Raises ValueError if the path escapes it.
    """
    if not JOPLIN_IMPORT_ROOT:
        raise ValueError(
            "Markdown import is disabled on this server. Set JOPLIN_IMPORT_ROOT "
            "to a directory to enable it."
        )

    root = Path(JOPLIN_IMPORT_ROOT).resolve()
    candidate = (root / raw_path).resolve() if not Path(raw_path).is_absolute() \
        else Path(raw_path).resolve()

    if candidate != root and root not in candidate.parents:
        raise ValueError(
            f"Refusing to read outside the import root. Allowed root: {root}"
        )

    return candidate

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

    if permanent and not JOPLIN_ALLOW_PERMANENT_DELETE:
        # Refused rather than downgraded to a trash delete, so the caller is not
        # told something happened that did not.
        return {
            "error": (
                "Permanent deletion is disabled on this server. Retry without "
                "permanent=true to move the note to the trash, where it can be "
                "recovered."
            )
        }

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

    Only files under the server's configured import root can be read. Paths are
    resolved before the check, so symlinks and `..` cannot escape it.

    Args:
        args: Import parameters
            file_path: Path to the markdown file, relative to the import root

    Returns:
        Dictionary containing the created note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        file_path = resolve_import_path(args.file_path)
    except ValueError as e:
        logger.warning("Rejected markdown import for %r: %s", args.file_path, e)
        return {"error": str(e)}

    try:
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


def apply_tool_policy() -> None:
    """Unregister tools the server-level policy forbids.

    Runs at import, so the policy holds for every transport rather than only the
    one `__main__` happens to start. Unregistering rather than refusing at call
    time means a forbidden tool is never advertised in the first place.
    """
    registered = [tool.name for tool in mcp._tool_manager.list_tools()]

    removed: list[str] = []

    if JOPLIN_READ_ONLY:
        for name in registered:
            if name not in READ_TOOLS:
                mcp.remove_tool(name)
                removed.append(name)
    elif not JOPLIN_IMPORT_ROOT and "import_markdown" in registered:
        # Already covered by the read-only branch when that is on.
        mcp.remove_tool("import_markdown")
        removed.append("import_markdown")

    if removed:
        logger.info("Tool policy removed %d tool(s): %s",
                    len(removed), ", ".join(sorted(removed)))

    logger.info(
        "Tool policy: read_only=%s, import_root=%s, allow_permanent_delete=%s, "
        "exposed=%s",
        JOPLIN_READ_ONLY,
        JOPLIN_IMPORT_ROOT or "<disabled>",
        JOPLIN_ALLOW_PERMANENT_DELETE,
        ", ".join(sorted(t.name for t in mcp._tool_manager.list_tools())),
    )


apply_tool_policy()


if __name__ == "__main__":
    logger.info(
        "Starting Joplin MCP Server (transport=%s, host=%s, port=%s)",
        MCP_TRANSPORT, MCP_HOST, MCP_PORT,
    )
    mcp.run(transport=MCP_TRANSPORT)
