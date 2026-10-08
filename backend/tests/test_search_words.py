"""Spaces, underscores and hyphens separate the same words in every search."""

import os
import tempfile
import unittest
from unittest.mock import patch

from app.core.database import connection, initialize
from app.pdfs.search import ranked_matches


class SeparatorTests(unittest.TestCase):
    def test_aws_for_ide_in_any_spelling(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OWL_DB_PATH": folder + "/owl.db"}):
            initialize()
            with connection() as db:
                db.execute("INSERT INTO tracked_projects(id,project_url,server,project) VALUES(1,'u','s','P')")
                db.execute("INSERT INTO repositories(id,project_id,repo,name) VALUES(1,1,'r','r')")
                for index, name in enumerate(["aws_for_ide.pdf", "aws-for-ide.pdf", "AWS for IDE.pdf", "other.pdf"], 1):
                    db.execute(
                        "INSERT INTO documents(id,repository_id,project,repo,pdf_name,path,url,file_size,page_count,"
                        "pdf_hash,pdf_text,added_at,updated_at,last_scanned) VALUES(?,1,'P','r',?,?,'u',1,1,'h','t','x','x','x')",
                        (index, name, name),
                    )
            for query in ("aws for ide", "aws_for_ide", "aws-for-ide", "AWS For IDE"):
                for mode in ("separate", "together"):
                    found = ranked_matches(query, ["name", "path"], mode)
                    self.assertEqual(sorted(found), [(1, 3), (2, 3), (3, 3)], (query, mode))


if __name__ == "__main__":
    unittest.main()
