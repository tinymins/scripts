import os
import logging
from datetime import datetime
from optparse import OptionParser

logging.basicConfig(filename='remove-empty-dirs.log', encoding='utf-8', level=logging.DEBUG)


def run(root_path, dry_run=False):
    if not dry_run:
        logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
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
