import os
import math
import pathlib
import logging
from datetime import datetime

logging.basicConfig(filename='arrange-photos.log', encoding='utf-8', level=logging.DEBUG)
logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

PHOTO_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif"]
VIDEO_EXTENSIONS = [".mp4", ".mov"]
ALL_EXTENSIONS = PHOTO_EXTENSIONS + VIDEO_EXTENSIONS

root_path = "."
for cwd, dirs, files in os.walk(root_path):
    for filename in files:
        file_ext = pathlib.Path(filename).suffix.lower()
        filename_without_ext = filename[:-len(file_ext)]
        filepath = os.path.abspath(os.path.join(cwd, filename))
        filepath_without_ext = os.path.abspath(os.path.join(cwd, filename_without_ext))

        if file_ext not in ALL_EXTENSIONS:
            continue

        mtime_file_path = filepath
        if file_ext in VIDEO_EXTENSIONS:
            for ext in PHOTO_EXTENSIONS:
                if os.path.isfile(filepath_without_ext + ext):
                    mtime_file_path = filepath_without_ext + ext
                    break
        if not os.path.isfile(mtime_file_path):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        new_filepath_without_ext = os.path.abspath(os.path.join(
            root_path,
            mtime.strftime('%Y') + 'Q' + str(math.floor((int(mtime.strftime('%m')) + 2) / 3)),
            mtime.strftime('%Y-%m-%d_%H-%M-%S')
        ))
        if new_filepath_without_ext == filepath_without_ext:
            continue

        conflict_pass = False
        conflict_number = 0
        conflict_string = ''
        while not conflict_pass:
            conflict_pass = True
            for ext in ALL_EXTENSIONS:
                if os.path.isfile(new_filepath_without_ext + conflict_string + ext) and new_filepath_without_ext + conflict_string + ext != filepath:
                    conflict_pass = False
                    conflict_number = conflict_number + 1
                    conflict_string = '_' + str(conflict_number)
                    break
        new_filepath_without_ext = new_filepath_without_ext + conflict_string

        if new_filepath_without_ext == filepath_without_ext:
            continue

        for ext in ALL_EXTENSIONS:
            if os.path.isfile(filepath_without_ext + ext):
                print("Arrange: " + filepath_without_ext + ext + " => " + new_filepath_without_ext + ext)
                pardir = os.path.abspath(os.path.join(new_filepath_without_ext, os.pardir))
                if not os.path.isdir(pardir):
                    logging.info('Makedir: ' + pardir)
                    os.makedirs(pardir)
                logging.info('Arrange: ' + filepath_without_ext + ext + " => " + new_filepath_without_ext + ext)
                os.rename(filepath_without_ext + ext, new_filepath_without_ext + ext)

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
