import os
import shutil
import subprocess
import stat  # To check file types more reliably potentially
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from flask import (Flask, abort, flash, redirect, render_template, request,
                   url_for, send_from_directory)

# --- Configuration ---
# IMPORTANT: Change this in a real application!
SECRET_KEY = 'your_very_secret_and_unguessable_key'
# IMPORTANT: Change this and keep it secret! Maybe load from environment.
SECURITY_TOKEN = 'supersecrettoken123'
# The root directory you want to browse. Make sure the Flask app has read/write permissions here.
# Use an absolute path for reliability.
# Example: ROOT_DIR = '/path/to/your/shared/files'
# For testing, you can use a temporary directory:
import tempfile
_temp_root = "models"
os.makedirs(_temp_root, exist_ok=True)
ROOT_DIR = os.path.abspath(_temp_root)

print(f"--- Serving files from: {ROOT_DIR} ---")
print(f"--- Security Token: {SECURITY_TOKEN} (Do NOT use this in production!) ---")
# Create a dummy file and folder for testing
Path(os.path.join(ROOT_DIR, "welcome.txt")).touch(exist_ok=True)
os.makedirs(os.path.join(ROOT_DIR, "example_subdir"), exist_ok=True)
Path(os.path.join(ROOT_DIR, "example_subdir", "nested_file.md")).touch(exist_ok=True)


# Directory for temporary Wget downloads
DOWNLOAD_TEMP_DIR = '/tmp'
os.makedirs(DOWNLOAD_TEMP_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- Helper Functions ---

def get_absolute_path(subpath):
    """Safely join the ROOT_DIR with the subpath and resolve."""
    # Decode URL encoding
    subpath = unquote(subpath)
    # Prevent path traversal: clean the subpath
    # os.path.normpath handles '.', '..', '//' etc.
    # We explicitly disallow '..' components at the start or middle after normalization
    clean_subpath = os.path.normpath(subpath).lstrip('/') # Remove leading slash if any
    if '..' in clean_subpath.split(os.path.sep):
         # If '..' exists anywhere in the components, it's suspicious after normpath
         # unless it resolves *within* the intended root, which abspath handles.
         # We'll double-check containment later.
         pass # Let abspath handle resolution, then check containment

    # Join and resolve to absolute path (handles symlinks, ..)
    absolute_path = os.path.abspath(os.path.join(ROOT_DIR, clean_subpath))
    return absolute_path

def is_safe_path(absolute_path):
    """Check if the absolute path is within the ROOT_DIR."""
    # Ensure the resolved path is still within the ROOT_DIR
    return absolute_path.startswith(ROOT_DIR)

def get_relative_path(absolute_path):
    """Get path relative to ROOT_DIR."""
    if absolute_path == ROOT_DIR:
        return ""
    # Add 1 to remove leading slash if present after stripping root dir
    rel_path = absolute_path[len(ROOT_DIR):].lstrip(os.path.sep)
    return rel_path

def get_dir_contents(current_abs_path):
    """Get list of files and directories in the current path."""
    items = []
    try:
        for name in sorted(os.listdir(current_abs_path), key=str.lower):
            item_abs_path = os.path.join(current_abs_path, name)
            try:
                # Use lstat to avoid following symlinks for type check if desired,
                # but os.path.isdir/isfile usually work fine and follow links.
                # is_dir = os.path.isdir(item_abs_path)
                # is_file = os.path.isfile(item_abs_path)

                # Let's use stat to be slightly more robust about types
                mode = os.stat(item_abs_path).st_mode
                is_dir = stat.S_ISDIR(mode)
                is_file = stat.S_ISREG(mode)
                # Could also check for symlinks: is_link = stat.S_ISLNK(mode)

                if is_dir or is_file: # Only list regular files and directories
                    items.append({
                        'name': name,
                        'abs_path': item_abs_path,
                        'rel_path': quote(get_relative_path(item_abs_path)), # URL-encode relative path
                        'is_dir': is_dir,
                    })
            except OSError:
                # Skip files/dirs we don't have permission to access or broken links
                continue
    except OSError as e:
        flash(f"Error accessing directory: {e}", "danger")
        return None # Indicate error
    return items

def get_breadcrumbs(current_rel_path):
    """Generate breadcrumbs for navigation."""
    breadcrumbs = []
    path_parts = current_rel_path.strip(os.path.sep).split(os.path.sep)
    if path_parts == ['']: # Handle root case
        return []

    path_so_far = ""
    for i, part in enumerate(path_parts):
        if i > 0:
            path_so_far += os.path.sep + part
        else:
            path_so_far = part
        breadcrumbs.append((part, quote(path_so_far))) # URL-encode path
    return breadcrumbs

def get_all_subdirs(root):
    """Recursively find all subdirectories relative to the root."""
    subdirs = []
    for dirpath, dirnames, _ in os.walk(root):
        # Exclude the root itself if needed, handled by relpath logic
        if dirpath != root:
            rel_dir = os.path.relpath(dirpath, root)
            subdirs.append(rel_dir)
    return sorted(subdirs)

def validate_token():
    """Check if the security token in the request args is valid."""
    token = request.args.get('token')
    if not token or token != SECURITY_TOKEN:
        app.logger.warning(f"Invalid or missing security token attempt from {request.remote_addr}")
        abort(403, "Forbidden: Invalid or missing security token.")


# --- Routes ---

@app.route('/')
def index():
    """Redirect root URL to the browser."""
    return redirect(url_for('browse'))

@app.route('/browse/', defaults={'subpath': ''})
@app.route('/browse/<path:subpath>')
def browse(subpath):
    """Main route for browsing files and folders."""
    current_abs_path = get_absolute_path(subpath)

    if not is_safe_path(current_abs_path) or not os.path.exists(current_abs_path):
        app.logger.warning(f"Attempt to access unsafe or non-existent path: {current_abs_path} from subpath: {subpath}")
        abort(404, "Path not found or access denied.")

    if not os.path.isdir(current_abs_path):
         # If it's a file, maybe offer download? For now, just error.
         abort(400, "Path is not a directory.")

    items = get_dir_contents(current_abs_path)
    if items is None: # Error occurred during listing
         # Flash message already set by get_dir_contents
         # Redirect to parent or root? Let's try parent.
         parent_rel = os.path.dirname(subpath.strip('/'))
         return redirect(url_for('browse', subpath=parent_rel))


    current_rel_path = get_relative_path(current_abs_path)
    breadcrumbs = get_breadcrumbs(current_rel_path)

    # Determine parent path for ".." link
    parent_path = None
    if current_abs_path != ROOT_DIR:
        parent_abs_path = os.path.dirname(current_abs_path)
        parent_path = quote(get_relative_path(parent_abs_path)) # URL-encode

    # Display path relative to root in UI
    current_path_display = f"Root/{current_rel_path}" if current_rel_path else "Root"

    return render_template(
        'browse.html',
        items=items,
        current_path_display=current_path_display,
        current_rel_path=quote(current_rel_path),
        parent_path=parent_path,
        breadcrumbs=breadcrumbs,
        token=SECURITY_TOKEN # Pass token for delete links
    )

@app.route('/delete/<path:subpath>')
def delete_item(subpath):
    """Route to delete a file or folder."""
    validate_token() # Check token first

    item_abs_path = get_absolute_path(subpath)
    item_rel_path = get_relative_path(item_abs_path) # Get relative path before potential deletion

    if not is_safe_path(item_abs_path) or item_abs_path == ROOT_DIR:
        app.logger.warning(f"Attempt to delete unsafe path or root: {item_abs_path}")
        flash("Cannot delete this item (unsafe path or root directory).", "danger")
        return redirect(url_for('browse')) # Redirect to root

    parent_dir_rel_path = os.path.dirname(item_rel_path)

    try:
        if os.path.isdir(item_abs_path):
            shutil.rmtree(item_abs_path)
            flash(f"Directory '{os.path.basename(item_abs_path)}' deleted successfully.", "success")
            app.logger.info(f"Deleted directory: {item_abs_path}")
        elif os.path.isfile(item_abs_path):
            os.remove(item_abs_path)
            flash(f"File '{os.path.basename(item_abs_path)}' deleted successfully.", "success")
            app.logger.info(f"Deleted file: {item_abs_path}")
        else:
             # Could be symlink, socket, etc. or doesn't exist
             if os.path.exists(item_abs_path) or os.path.islink(item_abs_path):
                 # Try removing if it exists (might be a broken link)
                 os.remove(item_abs_path)
                 flash(f"Item '{os.path.basename(item_abs_path)}' deleted successfully.", "success")
                 app.logger.info(f"Deleted item (link?): {item_abs_path}")
             else:
                 flash(f"Item '{os.path.basename(item_abs_path)}' not found.", "warning")
                 app.logger.warning(f"Attempt to delete non-existent item: {item_abs_path}")


    except OSError as e:
        flash(f"Error deleting '{os.path.basename(item_abs_path)}': {e}", "danger")
        app.logger.error(f"Error deleting {item_abs_path}: {e}")
    except Exception as e:
        flash(f"An unexpected error occurred during deletion: {e}", "danger")
        app.logger.error(f"Unexpected error deleting {item_abs_path}: {e}")

    # Redirect to the parent directory after deletion
    return redirect(url_for('browse', subpath=quote(parent_dir_rel_path)))

@app.route('/download', methods=['GET', 'POST'])
def download_ui():
    """Route for the download and link creation UI."""
    if request.method == 'POST':
        validate_token() # Check token for POST request

        url = request.form.get('url')
        filename = request.form.get('filename')
        target_dir_rel = request.form.get('target_dir_rel') # Relative to ROOT_DIR

        # --- Input Validation ---
        if not url or not filename or target_dir_rel is None: # Check target_dir_rel explicitly for empty string case
            flash("Missing required fields (URL, Filename, Target Directory).", "danger")
            return redirect(url_for('download_ui'))

        # Validate filename (basic check for disallowed chars)
        if '/' in filename or '\\' in filename or '..' in filename or not filename.replace('.', '').replace('_', '').replace('-', '').isalnum():
             flash(f"Invalid filename '{filename}'. Use only letters, numbers, dot, underscore, hyphen.", "danger")
             return redirect(url_for('download_ui'))

        # Validate target directory
        target_dir_abs = get_absolute_path(target_dir_rel) # Handles '.' correctly for root
        if not is_safe_path(target_dir_abs) or not os.path.isdir(target_dir_abs):
            flash(f"Invalid target directory selected.", "danger")
            app.logger.warning(f"Invalid target directory selected: rel='{target_dir_rel}', abs='{target_dir_abs}'")
            return redirect(url_for('download_ui'))

        # --- Prepare paths ---
        download_dest_path = os.path.join(DOWNLOAD_TEMP_DIR, filename)
        link_target_path = os.path.join(target_dir_abs, filename)

        # Check if link already exists (optional, decide behavior: overwrite or fail)
        if os.path.lexists(link_target_path): # Use lexists to check link itself
            flash(f"A file or link named '{filename}' already exists in the target directory. Please choose a different filename or target.", "warning")
            return redirect(url_for('download_ui'))

        # --- Execute Wget ---
        wget_url = f"{url}?token={SECURITY_TOKEN}" # Append token to URL
        # Use -O for specific output file path in /tmp
        wget_command = ['wget', '-P', DOWNLOAD_TEMP_DIR, '-O', download_dest_path, '--no-verbose', '--tries=3', wget_url]
        app.logger.info(f"Running wget command: {' '.join(wget_command)}") # Log command without token if sensitive

        try:
            # Using check=True raises CalledProcessError on non-zero exit status
            # Capture output for potential debugging
            result = subprocess.run(wget_command, check=True, capture_output=True, text=True, timeout=300) # 5 min timeout
            app.logger.info(f"Wget successful for {url}. Output:\n{result.stdout}\n{result.stderr}")
            flash(f"File downloaded successfully to {download_dest_path}.", "info")

        except subprocess.CalledProcessError as e:
            flash(f"Wget download failed (Exit Code {e.returncode}). Error: {e.stderr}", "danger")
            app.logger.error(f"Wget failed for {url}. Command: {' '.join(e.cmd)}. Exit Code: {e.returncode}. Stderr: {e.stderr}")
            # Clean up potentially partially downloaded file
            if os.path.exists(download_dest_path):
                 try:
                     os.remove(download_dest_path)
                 except OSError:
                     pass
            return redirect(url_for('download_ui'))
        except subprocess.TimeoutExpired:
             flash("Wget download timed out.", "danger")
             app.logger.error(f"Wget timed out for {url}.")
             # Clean up potentially partially downloaded file
             if os.path.exists(download_dest_path):
                  try:
                      os.remove(download_dest_path)
                  except OSError:
                      pass
             return redirect(url_for('download_ui'))
        except FileNotFoundError:
            flash("Error: 'wget' command not found. Is wget installed and in the system's PATH?", "danger")
            app.logger.error("wget command not found.")
            return redirect(url_for('download_ui'))
        except Exception as e:
             flash(f"An unexpected error occurred during download: {e}", "danger")
             app.logger.error(f"Unexpected error during wget execution for {url}: {e}")
             # Clean up potentially partially downloaded file
             if os.path.exists(download_dest_path):
                 try:
                     os.remove(download_dest_path)
                 except OSError:
                     pass
             return redirect(url_for('download_ui'))


        # --- Create Symbolic Link ---
        try:
            os.symlink(download_dest_path, link_target_path)
            flash(f"Symbolic link created successfully: '{os.path.join(target_dir_rel, filename)}' -> '{download_dest_path}'", "success")
            app.logger.info(f"Created symlink: {link_target_path} -> {download_dest_path}")
            # Redirect to the browse page of the target directory
            return redirect(url_for('browse', subpath=quote(target_dir_rel)))

        except OSError as e:
            flash(f"Failed to create symbolic link: {e}", "danger")
            app.logger.error(f"Failed to create symlink {link_target_path} -> {download_dest_path}: {e}")
            # Don't delete the downloaded file here, user might want to retry linking manually
            # But inform them it's in /tmp
            flash(f"The downloaded file is available at {download_dest_path}", "info")
            return redirect(url_for('download_ui'))
        except Exception as e:
             flash(f"An unexpected error occurred during link creation: {e}", "danger")
             app.logger.error(f"Unexpected error creating symlink {link_target_path}: {e}")
             flash(f"The downloaded file is available at {download_dest_path}", "info")
             return redirect(url_for('download_ui'))

    else: # GET Request
        # Prepare data for the form
        available_dirs = get_all_subdirs(ROOT_DIR)
        root_dir_short_name = os.path.basename(ROOT_DIR) # Just show the last part of the root path

        return render_template(
            'download.html',
            available_dirs=available_dirs,
            token=SECURITY_TOKEN,
            temp_dir=DOWNLOAD_TEMP_DIR,
            root_dir_short=root_dir_short_name
        )


if __name__ == '__main__':
    # Set debug=True for development, False for production
    # Use host='0.0.0.0' to make it accessible on your network
    app.run(debug=True, host='0.0.0.0', port=5000)