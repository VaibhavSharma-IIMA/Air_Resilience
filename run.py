#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""Convenience shim so the CLI runs from a checkout without installing.

Installed users get the `airresilience` command instead.
"""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from airresilience.cli import main

if __name__ == "__main__":
    main()
