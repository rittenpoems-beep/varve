#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check-env.py — 环境能力检查（供 install.ps1 / doctor.ps1 调用，避免 PowerShell 引号转义问题）。

输出（每行 键=值）:
  python=3.12
  sqlite=3.49.1
  fts5=ok|missing
  trigram=ok|missing
"""
import sqlite3
import sys

print("python=" + "%d.%d" % sys.version_info[:2])
print("sqlite=" + sqlite3.sqlite_version)

try:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t_fts USING fts5(x)")
    con.close()
    print("fts5=ok")
except Exception:
    print("fts5=missing")

try:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t_tri USING fts5(x, tokenize='trigram')")
    con.close()
    print("trigram=ok")
except Exception:
    print("trigram=missing")
