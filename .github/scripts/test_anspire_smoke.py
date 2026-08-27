#!/usr/bin/env python3
"""Anspire Search API smoke test — no stock analysis, no LLM calls.

Usage:
    ANSPIRE_API_KEYS=sk-xxx python .github/scripts/test_anspire_smoke.py

Expected output:
    ✅ Key exists
    ✅ HTTP 200
    ✅ results: N
    ⏺ First title: xxx
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

API_URL = "https://plugin.anspire.cn/api/ntsearch/search"
QUERY = "贵州茅台 600519"


def mask_key(raw: str) -> str:
    """Show only first 4 and last 4 chars of an API key."""
    if len(raw) <= 12:
        return raw[:4] + "****"
    return raw[:4] + "****" + raw[-4:]


def main():
    raw_keys = os.getenv("ANSPIRE_API_KEYS", "").strip()
    if not raw_keys:
        print("❌ ANSPIRE_API_KEYS is not set or empty")
        sys.exit(1)

    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not keys:
        print("❌ ANSPIRE_API_KEYS contains no valid key entries")
        sys.exit(1)

    key = keys[0]
    print(f"✅ Key exists: {mask_key(key)}")
    print(f"✅ Total keys configured: {len(keys)}")

    now = datetime.now()
    seven_days_ago = now - timedelta(days=7)

    headers = {"Authorization": f"Bearer {key}"}
    params = {
        "query": QUERY,
        "top_k": 5,
        "FromTime": seven_days_ago.strftime("%Y-%m-%d %H:%M:%S"),
        "ToTime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "region_mode": 2,
    }

    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
    except requests.exceptions.Timeout:
        print("❌ HTTP timeout (15s)")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Connection error: {e}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")
        sys.exit(1)

    print(f"✅ HTTP {resp.status_code}")

    if resp.status_code != 200:
        try:
            if resp.headers.get("content-type", "").startswith("application/json"):
                err_data = resp.json()
                msg = err_data.get("message", err_data.get("msg", resp.text))
            else:
                msg = resp.text
        except Exception:
            msg = resp.text

        if resp.status_code == 401:
            print(f"❌ API KEY 无效: {msg}")
        elif resp.status_code == 403:
            print(f"❌ 余额不足或权限不足: {msg}")
        elif resp.status_code == 400:
            print(f"❌ 请求参数错误: {msg}")
        else:
            print(f"❌ HTTP {resp.status_code}: {msg}")
        sys.exit(1)

    try:
        data = resp.json()
    except ValueError as e:
        print(f"❌ Response JSON parse failed: {e}")
        sys.exit(1)

    if "code" in data and data.get("code") != 200:
        msg = data.get("msg", f"error code: {data.get('code')}")
        print(f"❌ API error: {msg}")
        sys.exit(1)

    results = data.get("results", [])
    if not results:
        print("❌ No results returned")
        # Print partial response for debugging (no key exposure)
        safe_debug = {k: v for k, v in data.items() if k != "results"}
        print(f"   Response keys: {list(data.keys())}")
        print(f"   Metadata: {json.dumps(safe_debug, ensure_ascii=False)}")
        sys.exit(1)

    print(f"✅ results: {len(results)}")
    first = results[0]
    title = first.get("title", first.get("name", "(no title)"))
    print(f"⏺ First title: {title}")

    # Optional: show source/date for extra context
    source = first.get("source") or first.get("from") or ""
    pub_date = first.get("date") or first.get("pubDate") or ""
    if source:
        print(f"   Source: {source}")
    if pub_date:
        print(f"   Date: {pub_date}")

    print("\n🎉 Anspire Search smoke test passed")
    sys.exit(0)


if __name__ == "__main__":
    main()