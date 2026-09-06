import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app

class WorkspaceTests(unittest.TestCase):
    def test_bookmark_roundtrip_and_conflict(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'OWL_DB_PATH':folder+'/db'}):
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/workspace').json(),{'projects':[],'documents':[],'people':[]})
                payload=client.get('/api/bookmarks/workspace').json()
                payload['bookmarks']=[{'id':13,'title':'Real link','url':'https://example.org','views':2}]
                payload['notes']={'13':'My note'}
                self.assertEqual(client.put('/api/bookmarks/workspace',json=payload).status_code,200)
                self.assertEqual(client.put('/api/bookmarks/workspace',json=payload).status_code,409)
            with TestClient(app) as client:
                saved=client.get('/api/bookmarks/workspace').json()
                self.assertEqual(saved['bookmarks'][0]['title'],'Real link')
                self.assertEqual(saved['notes']['13'],'My note')
