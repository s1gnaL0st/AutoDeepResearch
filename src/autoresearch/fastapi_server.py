"""Uvicorn entry point for the FastAPI control-plane facade."""
from __future__ import annotations
import argparse
import uvicorn
from .api import create_fastapi_app

def main() -> None:
    parser = argparse.ArgumentParser(prog="autoresearch-fastapi")
    parser.add_argument("--store", default=".autoresearch")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run(create_fastapi_app(args.store), host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
