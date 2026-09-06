"""Interactive PDF search using the same index as the API."""

from app.core.database import initialize
from app.pdfs.search import search_documents

if __name__ == "__main__":
    initialize()
    while True:
        try:
            query = input("Search (exit to quit)> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if query.lower() == "exit":
            break
        if query:
            for row in search_documents(query):
                print(
                    f"{row['pdf_name']} | {row['project']}/{row['repo']} | {row['author']}\n{row['url']}"
                )
