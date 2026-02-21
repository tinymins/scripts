import os
import math
import pathlib
import logging
import re
from datetime import datetime
from optparse import OptionParser

logging_file = os.path.abspath(os.path.splitext(__file__)[0] + '.log')
logging.basicConfig(filename=logging_file, encoding='utf-8', level=logging.DEBUG)

ALL_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".tga", ".heic", ".mp4", ".mov"]


def run(root_path, dry_run=False):
    for cwd, dirs, files in os.walk(root_path):
        if not dry_run:
            logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
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

            pardir = os.path.abspath(os.path.join(new_filepath, os.pardir))
            if not os.path.isdir(pardir):
                print("Makedir: " + pardir)
                if not dry_run:
                    logging.info('Makedir: ' + pardir)
                    os.makedirs(pardir)
            print("Arrange: " + filepath + " => " + new_filepath)
            if not dry_run:
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
                if not dry_run:
                    logging.info('Remove: ' + dirpath)
                    os.rmdir(dirpath)


if __name__ == '__main__':
    parser = OptionParser()
    parser.add_option('--dry-run', action='store_true', dest='dry_run', help='Dry run')
    (options, args) = parser.parse_args()
    if len(args) != 1:
        parser.error("incorrect number of arguments")
    if options.dry_run:
        print("> DRY RUN MODE")
    try:
        run(args[0], options.dry_run)
    except KeyboardInterrupt:
        exit()
