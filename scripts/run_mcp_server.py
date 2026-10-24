"""Launch the platform-neutral ABGES MCP server using stdio transport."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abges_mcp.server import main


if __name__ == "__main__":
    main()
