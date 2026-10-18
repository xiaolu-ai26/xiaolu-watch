#!/usr/bin/env python3
"""Local-only Phone onboarding issuance. Never print code/token or write them to logs."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from context_store import ContextStore,atomic_json

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path);p.add_argument('--output',required=True,type=Path);args=p.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf-8'));endpoint=cfg['endpoint'].rstrip('/');url=urlsplit(endpoint)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:raise ValueError('Expected HTTPS origin')
    # Output must be in the configured protected private context root. The installer
    # may transport this bounded, short-lived file through its private workspace.
    root=Path(cfg['context_root']).resolve();out=args.output.absolute()
    if not out.parent.exists() or out.parent.resolve()!=root or out.is_symlink():raise ValueError('Output must be directly inside protected context root')
    issued=ContextStore(root).issue_pair();atomic_json(out,{'endpoint':endpoint,'code':issued['code']})
    print(json.dumps({'created':True,'expires_at':issued['expires_at'],'purpose':'phone-v3'}))
if __name__=='__main__':main()
