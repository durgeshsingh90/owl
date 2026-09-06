"""Launch a crawl through the running API so only one worker owns the index."""

import argparse
import time

import httpx

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project_url")
    parser.add_argument("--api", default="http://127.0.0.1:8000/api")
    args = parser.parse_args()
    with httpx.Client(timeout=30) as client:
        result = client.post(
            args.api + "/project", json={"project_url": args.project_url}
        )
        result.raise_for_status()
        project = result.json()
        result = client.post(args.api + "/crawl", json={"project_ids": [project["id"]]})
        result.raise_for_status()
        job = result.json()
        while job["status"] in ("queued", "running"):
            time.sleep(1)
            result = client.get(args.api + "/jobs/" + job["id"])
            result.raise_for_status()
            job = result.json()
            print(job)
        raise SystemExit(0 if job["status"] == "succeeded" else 1)
