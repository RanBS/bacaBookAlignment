import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from baca.app import Baca
from baca.db import migrate
from baca.exceptions import EbookNotFound, FormatNotSupported
from baca.utils.cli import find_file, get_ebook_class


def main():
    console = Console()
    try:
        migrate()

        # 1. Get all paths from command line arguments
        # sys.argv[1:] takes everything after 'python main.py'
        args = sys.argv[1:]

        ebook_paths = []

        if not args:
            # Fallback to the original find_file() if no args provided
            # (This usually opens the last read book)
            ebook_paths.append(find_file())
        else:
            for arg in args:
                path = Path(arg)
                if path.exists():
                    ebook_paths.append(path)
                else:
                    console.print(Text(f"File not found: {arg}", style="bold red"))

        if not ebook_paths:
            raise EbookNotFound("No valid ebook files provided.")

        # 2. Assume the first book's format defines the ebook_class
        # (Usually 'Epub', but this keeps it flexible)
        ebook_class = get_ebook_class(ebook_paths[0])

        # 3. Launch Baca with the LIST of paths
        return sys.exit(Baca(ebook_paths=ebook_paths, ebook_class=ebook_class).run())

    except (EbookNotFound, FormatNotSupported) as e:
        console.print(Text(str(e), style="bold red"))
        sys.exit(-1)
    except Exception:
        console.print_exception()
        sys.exit(-1)


if __name__ == "__main__":
    main()