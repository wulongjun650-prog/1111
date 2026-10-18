#!/usr/bin/env python3
"""Paginate public GCS bucket via cdn.genspicy.ai ListBucket XML."""

import argparse
import sys
import time
import xml.etree.ElementTree as ET

import requests

LIST_URL = "https://cdn.genspicy.ai/"
NS = {"s3": "http://doc.s3.amazonaws.com/2006-03-01"}


def list_all(max_keys: int = 1000, delimiter: str | None = None, output: str | None = None) -> tuple[list[str], list[str]]:
    """Return (object_keys, common_prefixes). Streams keys to output file if given."""
    objects: list[str] = []
    prefixes: list[str] = []
    marker = None
    session = requests.Session()
    out_f = open(output, "w", encoding="utf-8") if output else None
    try:
        while True:
            params: dict = {"max-keys": str(max_keys)}
            if delimiter:
                params["delimiter"] = delimiter
            if marker:
                params["marker"] = marker
            r = session.get(LIST_URL, params=params, timeout=120)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for c in root.findall("s3:Contents/s3:Key", NS):
                if c.text:
                    objects.append(c.text)
                    if out_f:
                        out_f.write(c.text + "\n")
                        out_f.flush()
            for p in root.findall("s3:CommonPrefixes/s3:Prefix", NS):
                if p.text:
                    prefixes.append(p.text)
            truncated = root.find("s3:IsTruncated", NS)
            is_truncated = truncated is not None and truncated.text == "true"
            if not is_truncated:
                break
            next_marker = root.find("s3:NextMarker", NS)
            if next_marker is not None and next_marker.text:
                marker = next_marker.text
            elif objects:
                marker = objects[-1]
            else:
                break
            time.sleep(0.02)
    finally:
        if out_f:
            if prefixes:
                out_f.write("\n# CommonPrefixes\n")
                for p in prefixes:
                    out_f.write(p + "\n")
            out_f.close()
    return objects, prefixes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--delimiter", default=None)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    objs, prefixes = list_all(delimiter=args.delimiter, output=args.output)
    if args.summary:
        print(f"objects={len(objs)} prefixes={len(prefixes)}", file=sys.stderr)
    print(len(objs))


if __name__ == "__main__":
    main()
