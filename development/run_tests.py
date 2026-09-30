#!/usr/bin/env python3
"""Run portable, offline regression tests with isolated synthetic fixtures."""
from pathlib import Path
import argparse
import os
import tempfile
import unittest
import sys

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',help='Keep synthetic forward-test artifacts in this directory')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    scripts=root/'skill'/'canvas-notion-study'/'scripts'
    sys.path.insert(0,str(scripts))
    os.environ['CANVAS_NOTION_STUDY_SCRIPTS']=str(scripts)
    os.environ['PYTHONPATH']=str(scripts)+os.pathsep+os.environ.get('PYTHONPATH','')
    with tempfile.TemporaryDirectory(prefix='canvas-notion-study-tests-') as temporary:
        os.environ['CANVAS_NOTION_STUDY_FORWARD_WORKSPACE']=str(Path(args.work_dir).resolve()) if args.work_dir else temporary
        suite=unittest.defaultTestLoader.discover(str(root/'development'/'tests'))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

if __name__=='__main__':
    raise SystemExit(main())
