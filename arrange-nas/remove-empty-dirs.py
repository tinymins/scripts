import os
import logging
from datetime import datetime

logging.basicConfig(filename='remove-empty-dirs.log', encoding='utf-8', level=logging.DEBUG)
logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

root_path = "."
for cwd, dirs, files in os.walk(root_path, False):
    for dirname in dirs:
        is_empty = True
        dirpath = os.path.abspath(os.path.join(cwd, dirname))
        for child_cwd, child_dirs, child_files in os.walk(dirpath):
            if not is_empty:
                break
            for _ in child_files:
                is_empty = False
                break
        if is_empty and os.path.isdir(dirpath):
            print("Remove: " + dirpath)
            logging.info('Remove: ' + dirpath)
            os.rmdir(dirpath)
