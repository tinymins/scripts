# pip install Pillow pillow-heif
import os
import math
import pathlib
import logging
from PIL import Image, ExifTags
from pillow_heif import register_heif_opener
from datetime import datetime
from optparse import OptionParser

logging.basicConfig(filename='arrange-photos.log', encoding='utf-8', level=logging.DEBUG)
logging.info('Start: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
register_heif_opener()

PHOTO_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".tga", ".heic"]
VIDEO_EXTENSIONS = [".mp4", ".mov"]
ALL_EXTENSIONS = PHOTO_EXTENSIONS + VIDEO_EXTENSIONS


def get_mtime(mtime_file_path):
    try:
        image = Image.open(mtime_file_path)
        image.verify()
        exif = image.getexif()
        rtime = exif.get(ExifTags.Base.DateTimeOriginal) or exif.get(ExifTags.Base.DateTime)
        return (datetime.strptime(rtime, '%Y:%m:%d %H:%M:%S'), "EXIF_CTime")
    except KeyboardInterrupt as err:
        raise err
    except:
        pass
    return (datetime.fromtimestamp(os.path.getmtime(mtime_file_path)), "FILE_MTime")


def run(root_path):
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

            (mtime, mtimetype) = get_mtime(mtime_file_path)
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
                    if os.path.isfile(new_filepath_without_ext + conflict_string + ext) and new_filepath_without_ext + conflict_string != filepath_without_ext:
                        conflict_pass = False
                        conflict_number = conflict_number + 1
                        conflict_string = '_' + str(conflict_number)
                        break
            new_filepath_without_ext = new_filepath_without_ext + conflict_string

            if new_filepath_without_ext == filepath_without_ext:
                continue

            for ext in ALL_EXTENSIONS:
                if os.path.isfile(filepath_without_ext + ext):
                    print("Arrange: " + filepath_without_ext + ext + " => " + new_filepath_without_ext + ext + " (USING " + mtimetype + ")")
                    pardir = os.path.abspath(os.path.join(new_filepath_without_ext, os.pardir))
                    if not os.path.isdir(pardir):
                        logging.info('Makedir: ' + pardir)
                        os.makedirs(pardir)
                    if not options.dry_run:
                        logging.info('Arrange: ' + filepath_without_ext + ext + " => " + new_filepath_without_ext + ext + " (USING " + mtimetype + ")")
                        os.rename(filepath_without_ext + ext, new_filepath_without_ext + ext + " (USING " + mtimetype + ")")

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
                if not options.dry_run:
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
        run(args[0])
    except KeyboardInterrupt:
        exit()
