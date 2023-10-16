import os
import math
import pathlib
import logging
import re
from datetime import datetime

logging_file = os.path.abspath(os.path.splitext(__file__)[0] + '.log')
logging.basicConfig(filename=logging_file, encoding='utf-8', level=logging.DEBUG)

ALL_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".tga", ".heic", ".mp4", ".mov"]

root_path = "."
for cwd, dirs, files in os.walk(root_path):
    for filename in files:
        file_ext = pathlib.Path(filename).suffix.lower()
        filepath = os.path.abspath(os.path.join(cwd, filename))

        if file_ext not in ALL_EXTENSIONS:
            continue

        res = re.findall(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", filename)
        if len(res) == 0:
            continue
        year = int(res[0][0])
        month = int(res[0][1])
        new_filepath = os.path.abspath(os.path.join(
            root_path,
            str(year) + 'Q' + str(math.floor((month + 2) / 3)),
            filename
        ))

        if new_filepath == filepath:
            continue

        print("Arrange: " + filepath + " => " + new_filepath)
        pardir = os.path.abspath(os.path.join(new_filepath, os.pardir))
        if not os.path.isdir(pardir):
            logging.info('Makedir: ' + pardir)
            os.makedirs(pardir)
        logging.info('Arrange: ' + filepath + " => " + new_filepath)
        os.rename(filepath, new_filepath)

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
