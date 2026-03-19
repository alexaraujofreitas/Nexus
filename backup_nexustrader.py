import os
import shutil
from datetime import datetime

SOURCE_DIR = r"C:\Users\alexa\NexusTrader"
DEST_DIR = r"E:\NexusTrader_backup"


def backup_directory(src, dst):
    if not os.path.exists(src):
        print(f"Source directory does not exist: {src}")
        return

    if os.path.exists(dst):
        print("Removing previous backup...")
        shutil.rmtree(dst)

    print("Starting backup...")
    shutil.copytree(src, dst)

    print("Backup completed successfully.")


def main():
    print("=====================================")
    print(" NexusTrader Backup Script")
    print("=====================================")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Backup started at: {now}\n")

    backup_directory(SOURCE_DIR, DEST_DIR)

    print("\nBackup finished.")


if __name__ == "__main__":
    main()