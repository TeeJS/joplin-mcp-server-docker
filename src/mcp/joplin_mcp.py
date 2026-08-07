"""Joplin MCP Server implementation."""

import logging
import sys
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Add the src directory to the Python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.joplin.joplin_api import JoplinAPI, JoplinNotebook, JoplinNote, OrderDirection
from src.joplin.joplin_links import find_neighbours, get_link_graph
from src.joplin.joplin_utils import get_token_from_env, get_base_url_from_env, MarkdownContent

# Initialize FastMCP server
mcp = FastMCP("joplin")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Joplin API client
try:
    api = JoplinAPI(token=get_token_from_env(), base_url=get_base_url_from_env())
    logger.info(f"Successfully initialized Joplin API client (base_url={get_base_url_from_env()})")
except Exception as e:
    logger.error(f"Failed to initialize Joplin API client: {e}")
    api = None

# Tool parameters are declared as explicit primitive arguments (not a single
# pydantic-model argument). FastMCP in mcp>=1.x exposes a model-typed argument
# as a nested `{"args": {...}}` object, which broke MCP clients that pass the
# fields at the top level. Explicit args keep the tool schema flat and stable
# across mcp versions, so this works on both the host and in the devcontainer.


def serialize_notebook(notebook: JoplinNotebook) -> Dict[str, Any]:
    """Serialize a notebook tree into an MCP-friendly structure."""
    return {
        "id": notebook.id,
        "title": notebook.title,
        "parent_id": notebook.parent_id,
        "created_time": notebook.created_time.isoformat() if notebook.created_time else None,
        "updated_time": notebook.updated_time.isoformat() if notebook.updated_time else None,
        "user_created_time": notebook.user_created_time.isoformat() if notebook.user_created_time else None,
        "user_updated_time": notebook.user_updated_time.isoformat() if notebook.user_updated_time else None,
        "is_shared": notebook.is_shared,
        "share_id": notebook.share_id,
        "children": [serialize_notebook(child) for child in notebook.children or []],
    }


def iter_notebooks_with_paths(
    notebooks: List[JoplinNotebook],
    prefix: Optional[str] = None
) -> List[tuple[JoplinNotebook, str]]:
    """Flatten a notebook tree into notebook/path pairs."""
    flattened = []

    for notebook in notebooks:
        path = f"{prefix}/{notebook.title}" if prefix else notebook.title
        flattened.append((notebook, path))
        flattened.extend(iter_notebooks_with_paths(notebook.children or [], path))

    return flattened


def resolve_notebook_id(parent_id: Optional[str], notebook_name: Optional[str]) -> Optional[str]:
    """Resolve a destination notebook by ID or by exact title/path."""
    if parent_id and notebook_name:
        raise ValueError("Provide either parent_id or notebook_name, not both")

    if parent_id:
        return parent_id

    if not notebook_name:
        return None

    notebooks = api.list_notebooks()
    flattened = iter_notebooks_with_paths(notebooks)

    path_matches = [(notebook, path) for notebook, path in flattened if path == notebook_name]
    if len(path_matches) == 1:
        return path_matches[0][0].id
    if len(path_matches) > 1:
        matches = ", ".join(path for _, path in path_matches)
        raise ValueError(f"Notebook path '{notebook_name}' is ambiguous: {matches}")

    title_matches = [(notebook, path) for notebook, path in flattened if notebook.title == notebook_name]
    if len(title_matches) == 1:
        return title_matches[0][0].id
    if len(title_matches) > 1:
        matches = ", ".join(path for _, path in title_matches)
        raise ValueError(
            f"Notebook name '{notebook_name}' matches multiple notebooks. Use a full path instead: {matches}"
        )

    raise ValueError(f"Notebook '{notebook_name}' was not found")

# MCP Tools
@mcp.tool()
async def search_notes(query: str, limit: int = 100) -> Dict[str, Any]:
    """Search for notes in Joplin.

    Args:
        query: Search query string
        limit: Maximum number of results (default: 100)

    Returns:
        Dictionary containing search results
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        results = api.search_notes(query=query, limit=limit)
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
async def find_linked_notes(
    note_id: str,
    direction: str = "both",
    depth: int = 1,
    limit: int = 50,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Find notes connected to a note by Joplin's internal links.

    Answers both "what does this note link to" and "what links back to this
    note". Joplin's own search cannot do the latter at all, so this is the way
    to find the notes that reference something without knowing their wording.

    Useful after a search has surfaced one good note, to pull in the notes
    around it. Returns identifiers, titles and notebooks only - call get_note
    on whichever results are worth reading, so unrelated note bodies stay out
    of the conversation.

    Args:
        note_id: ID of the note to start from
        direction: "out" for links this note makes, "in" for links back to it,
            "both" to ignore direction (default: "both")
        depth: How many hops to follow, 1-3 (default: 1)
        limit: Maximum number of linked notes to return (default: 50)
        refresh: Rebuild the link graph instead of using the cached one

    Returns:
        Dictionary containing the origin note and the linked notes found
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        graph = get_link_graph(api, refresh=refresh)
        result = find_neighbours(
            graph, note_id=note_id, direction=direction, depth=depth, limit=limit,
        )
        return {"status": "success", **result}
    except KeyError:
        return {"error": f"Note {note_id} was not found"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Error finding linked notes: {e}")
        return {"error": str(e)}


@mcp.tool()
async def list_notebooks() -> Dict[str, Any]:
    """List available Joplin notebooks.

    Returns:
        Dictionary containing the notebook tree
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        notebooks = api.list_notebooks()
        return {
            "status": "success",
            "total": len(notebooks),
            "notebooks": [serialize_notebook(notebook) for notebook in notebooks],
        }
    except Exception as e:
        logger.error(f"Error listing notebooks: {e}")
        return {"error": str(e)}


@mcp.tool()
async def create_notebook(
    title: str,
    parent_id: Optional[str] = None,
    parent_notebook_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new Joplin notebook.

    Args:
        title: Notebook title
        parent_id: Parent notebook ID (optional)
        parent_notebook_name: Parent notebook name or path (optional)

    Returns:
        Dictionary containing the created notebook data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        parent_id = resolve_notebook_id(parent_id, parent_notebook_name)
        notebook = api.create_notebook(title=title, parent_id=parent_id)
        return {
            "status": "success",
            "notebook": serialize_notebook(notebook),
        }
    except Exception as e:
        logger.error(f"Error creating notebook: {e}")
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
async def create_note(
    title: str,
    body: Optional[str] = None,
    parent_id: Optional[str] = None,
    notebook_name: Optional[str] = None,
    is_todo: bool = False,
) -> Dict[str, Any]:
    """Create a new note in Joplin.

    Args:
        title: Note title
        body: Note content in Markdown (optional)
        parent_id: ID of parent folder (optional)
        notebook_name: Notebook title or full path (optional)
        is_todo: Whether this is a todo item (optional)

    Returns:
        Dictionary containing the created note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        parent_id = resolve_notebook_id(parent_id, notebook_name)
        note = api.create_note(
            title=title,
            body=body,
            parent_id=parent_id,
            is_todo=is_todo
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
async def update_note(
    note_id: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    parent_id: Optional[str] = None,
    notebook_name: Optional[str] = None,
    is_todo: Optional[bool] = None,
) -> Dict[str, Any]:
    """Update an existing note in Joplin.

    Args:
        note_id: ID of note to update
        title: New title (optional)
        body: New content (optional)
        parent_id: New parent folder ID (optional)
        notebook_name: New notebook title or full path (optional)
        is_todo: New todo status (optional)

    Returns:
        Dictionary containing the updated note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        parent_id = resolve_notebook_id(parent_id, notebook_name)
        note = api.update_note(
            note_id=note_id,
            title=title,
            body=body,
            parent_id=parent_id,
            is_todo=is_todo
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
async def import_markdown(
    file_path: str,
    notebook_name: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Import a markdown file as a new note.

    Args:
        file_path: Path to the markdown file
        notebook_name: Destination notebook title or full path (optional)
        parent_id: Destination notebook ID (optional)

    Returns:
        Dictionary containing the created note data
    """
    if not api:
        return {"error": "Joplin API client not initialized"}

    try:
        file_path = Path(file_path)
        md_content = MarkdownContent.from_file(file_path)
        parent_id = resolve_notebook_id(parent_id, notebook_name)
        
        note = api.create_note(
            title=md_content.title,
            body=md_content.content,
            parent_id=parent_id
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

if __name__ == "__main__":
    logging.info("Starting Joplin MCP Server...")
    mcp.run(transport='stdio')
