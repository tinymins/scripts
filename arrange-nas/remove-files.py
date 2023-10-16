import os
import logging
from datetime import datetime
from optparse import OptionParser

logging.basicConfig(filename='remove-files.log', encoding='utf-8', level=logging.DEBUG)

FILES = ['Thumbs.db']


def run(root_path, dry_run=False):
    if not dry_run:
        logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    for cwd, dirs, files in os.walk(root_path, False):
        for filename in files:
            filepath = os.path.abspath(os.path.join(cwd, filename))
            if filename in FILES:
                print("Remove: " + filepath)
                if not dry_run:
                    logging.info('Remove: ' + filepath)
                    os.remove(filepath)


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
