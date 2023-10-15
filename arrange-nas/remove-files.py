import os
import logging
from datetime import datetime

logging.basicConfig(filename='remove-files.log', encoding='utf-8', level=logging.DEBUG)
logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

FILES = ['Thumbs.db']

root_path = "."
for cwd, dirs, files in os.walk(root_path, False):
    for filename in files:
        filepath = os.path.abspath(os.path.join(cwd, filename))
        if filename in FILES:
            print("Remove: " + filepath)
            logging.info('Remove: ' + filepath)
            os.remove(filepath)
