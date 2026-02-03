#!/usr/bin/env python3
import os
import sys
import time
import hashlib
import re
import argparse
import html
import requests
import warnings
from urllib.parse import urlparse

# Suppress the LibreSSL/urllib3 warning for cleaner output
warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

class ChomikDownloader:
    """
    Chomikuj file downloader class ported from PHP.
    """

    def __init__(self, args):
        self.args = args
        self.user_name = args.user
        
        # Handle Password / Hash logic
        if args.hash:
            self.user_password_hash = args.hash
        else:
            # PHP: strtolower(md5($userPassword))
            encoded_pass = args.password.encode('utf-8')
            self.user_password_hash = hashlib.md5(encoded_pass).hexdigest().lower()

        self.exts = args.ext.split(',') if args.ext else []
        self.auth_token = None
        self.last_login_stamp = 0
        self.stamp = 0
        
        # Session to keep connections alive (simulating Connection: Keep-Alive)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0',
            'Accept-Language': 'pl-PL,en,*'
        })

    def log(self, message, error=False):
        """Simple CLI logger."""
        if not self.args.noprogress:
            target = sys.stderr if error else sys.stdout
            print(message, file=target)

    def login(self):
        """
        Logins or relogins user. Called automatically before download.
        """
        if self.last_login_stamp != 0 and time.time() < (self.last_login_stamp + 300):
            return True

        self.log(f"Logging in to chomikbox service as \"{self.user_name}\"... ", error=False)

        # Construct XML Payload manually to match legacy SOAP requirements exactly
        xml_data = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<Auth xmlns="http://chomikuj.pl/">'
            f'<name>{self.user_name}</name>'
            f'<passHash>{self.user_password_hash}</passHash>'
            '<ver>4</ver>'
            '<client>'
            '<name>chomikbox</name>'
            '<version>2.0.7.9</version>'
            '</client>'
            '</Auth>'
            '</s:Body>'
            '</s:Envelope>'
        )

        headers = {
            'SOAPAction': 'http://chomikuj.pl/IChomikBoxService/Auth',
            'Content-Type': 'text/xml;charset=utf-8',
        }

        # Use HTTPS
        response = self._request('https://box.chomikuj.pl/services/ChomikBoxService.svc', 
                                 method='POST', data=xml_data, headers=headers)

        if not response:
            self.log("Login Failed: No response.", error=True)
            return False

        # Extract token using regex
        token_match = re.search(r'<a:token>(.*?)</a:token>', response)
        status_match = re.search(r'<a:status>(.*?)</a:status>', response, re.DOTALL)

        if token_match:
            self.auth_token = token_match.group(1)
            self.last_login_stamp = time.time()
            status = status_match.group(1).upper() if status_match else "UNKNOWN"
            self.log(f"{status}.")
            return True
        
        self.log("Login Failed: Token not found.", error=True)
        return False

    def download_files_information(self, urls):
        """
        Retrieves file information about given URLs.
        """
        if not self.login():
            return []

        self.log("  Downloading files information for specified URLs:")
        for url in urls:
            self.log(f"    - {url}")

        files_info = []
        # We must avoid the "requested files from more than one folder" error
        # returned by the service when multiple paths are sent in one request.
        # To keep things simple and robust, request info for each URL separately
        # and aggregate the results.

        for url in urls:
            if not url:
                continue

            # Build single-entry list id from URL.
            parsed = urlparse(url)

            if not parsed.netloc:
                fixed_url = url
                if not fixed_url.startswith('/'):
                    fixed_url = '/' + fixed_url
                parsed = urlparse('https://chomikuj.pl' + fixed_url)

            path = parsed.path or '/'
            path = '/' + path.lstrip('/')

            self.log("  Preparing request to get information about files... ")

            self.stamp += 1
            entries = (
                '<DownloadReqEntry>'
                f'<id>{path}</id>'
                '</DownloadReqEntry>'
            )

            xml_data = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body>'
                '<Download xmlns="http://chomikuj.pl/">'
                f'<token>{self.auth_token}</token>'
                '<sequence>'
                f'<stamp>{self.stamp}</stamp>'
                '<part>0</part>'
                '<count>1</count>'
                '</sequence>'
                '<disposition>download</disposition>'
                '<list>'
                f'{entries}'
                '</list>'
                '</Download>'
                '</s:Body>'
                '</s:Envelope>'
            )

            # Debug: persist latest request XML for troubleshooting
            try:
                debug_req_path = os.path.join(os.getcwd(), "debug_download_info_request.xml")
                with open(debug_req_path, "w", encoding="utf-8") as f:
                    f.write(xml_data)
            except Exception:
                pass

            headers = {
                'SOAPAction': 'http://chomikuj.pl/IChomikBoxService/Download',
                'Content-Type': 'text/xml;charset=utf-8',
            }

            response = self._request('https://box.chomikuj.pl/services/ChomikBoxService.svc',
                                     method='POST', data=xml_data, headers=headers)

            self.log("OK.")

            # Debug: persist latest raw response so we can see actual structure
            try:
                debug_path = os.path.join(os.getcwd(), "debug_download_info.xml")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(response or "")
            except Exception:
                pass

            # Regex to parse file info.
            # NOTE: realId may be nil (`<realId i:nil="true"/>`), so we do NOT
            # rely on it being present as text content. The only things we
            # really need are numeric id, agreement name, cost, filename, size.
            # IMPORTANT: We must match <name> tags ONLY inside <FileEntry>, not the outer <DownloadFolder><name>.
            pattern = re.compile(
                r'<FileEntry[^>]*>.*?'
                r'<id>(\d+)</id>.*?'
                r'<agreementInfo>.*?<AgreementInfo><name>(.*?)</name><cost>(\d+)</cost>.*?</agreementInfo>.*?'
                r'(?:<realId[^>]*/>|</realId>).*?'
                r'<name>(.*?)</name><size>(\d+)</size>.*?'
                r'</FileEntry>',
                re.DOTALL
            )

            matches = pattern.findall(response or '')
            self.log(f"  Received {len(matches)} records with information about files.")

            for match in matches:
                fid, agreement, cost, name, size = match
                # Sanitize filename: remove any XML-like tags that might have leaked in.
                name = re.sub(r'<[^>]+>', '', name).strip()
                # Remove any leading/trailing XML fragments.
                name = re.sub(r'^[<>/]+', '', name)
                name = re.sub(r'[<>/]+$', '', name)
                size = int(size)
                ext = os.path.splitext(name)[1].lstrip('.').lower()

                # Filter Extensions
                if self.exts and ext not in [e.lower() for e in self.exts]:
                    self.log(f"Skipping file ({name}), because of extension ({ext}).")
                    continue

                # Filter Size
                if self.args.max_limit and size > int(self.args.max_limit):
                    self.log(f"Skipping file ({name}), because of size limit ({size}).")
                    continue

                files_info.append({
                    'id': fid,
                    'agreement': agreement,
                    'cost': cost,
                    'realId': None,
                    'name': name,
                    'size': size
                })

        self.log(f"  Total of {len(files_info)} files added to download queue.")
        on_listed = getattr(self.args, 'on_files_listed', None)
        if on_listed:
            try:
                on_listed([{'name': f['name'], 'size': f['size']} for f in files_info])
            except Exception:
                pass
        return files_info

    def download_files(self, urls, destination_folder=''):
        """
        Orchestrates the download process.
        """
        if not urls:
            self.log("  No URLs given to download.")
            return True

        if not self.login():
            return False

        # 1. Get Metadata
        files_info = self.download_files_information(urls)

        # 2. Iterate and Download
        iteration_size = 1 
        chunks = [files_info[i:i + iteration_size] for i in range(0, len(files_info), iteration_size)]

        for i, chunk in enumerate(chunks):
            self.log(f"  Download iteration {i + 1} / {len(chunks)}")
            
            entries = ''
            for file_info in chunk:
                entries += f"<DownloadReqEntry><id>{file_info['id']}</id><agreementInfo><AgreementInfo><name>{file_info['agreement']}</name>"
                if file_info['agreement'] != 'small':
                    entries += f"<cost>{file_info['cost']}</cost>"
                entries += "</AgreementInfo></agreementInfo></DownloadReqEntry>"

            self.stamp += 1
            xml_data = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body>'
                '<Download xmlns="http://chomikuj.pl/">'
                f'<token>{self.auth_token}</token>'
                '<sequence>'
                f'<stamp>{self.stamp}</stamp>'
                '<part>0</part>'
                '<count>1</count>'
                '</sequence>'
                '<disposition>download</disposition>'
                '<list>'
                f'{entries}'
                '</list>'
                '</Download>'
                '</s:Body>'
                '</s:Envelope>'
            )

            # Debug: persist request XML for troubleshooting
            try:
                debug_req_path = os.path.join(os.getcwd(), "debug_download_files_request.xml")
                with open(debug_req_path, "w", encoding="utf-8") as f:
                    f.write(xml_data)
            except Exception:
                pass

            headers = {
                'SOAPAction': 'http://chomikuj.pl/IChomikBoxService/Download',
                'Content-Type': 'text/xml;charset=utf-8',
            }

            response = self._request('https://box.chomikuj.pl/services/ChomikBoxService.svc',
                                     method='POST', data=xml_data, headers=headers)

            # Debug: persist raw response from download request
            try:
                debug_path = os.path.join(os.getcwd(), "debug_download_files.xml")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(response or "")
            except Exception:
                pass

            global_id_match = re.search(r'<globalId>(.*?)</globalId>', response or '')
            path_prefix = ''
            if global_id_match:
                raw_path = global_id_match.group(1)  # e.g. /Pepe2020/Ebooki/File.ext
                # Strip leading slash.
                raw_path = raw_path.lstrip('/')
                # Use only the directory component (drop the filename itself).
                dir_part = os.path.dirname(raw_path)
                # Remove non-ASCII noise just in case.
                path_prefix = re.sub(r'[^\x20-\x7F]', '', dir_part)

            # Parse actual download URLs from FileEntry.
            # IMPORTANT: Match <name> tags ONLY inside <FileEntry>, not outer <DownloadFolder><name>.
            # realId can be i:nil="true", so we ignore it and just pick:
            #   - numeric id
            #   - file name
            #   - size
            #   - optional url (may be nil)
            file_pattern = re.compile(
                r'<FileEntry[^>]*>.*?'
                r'<id>(\d+)</id>.*?'
                r'(?:<realId[^>]*/>|</realId>).*?'
                r'<name>(.*?)</name><size>(\d+)</size>.*?'
                r'(?:<url i:nil="true"\s*/>|<url>(.*?)</url>).*?'
                r'</FileEntry>',
                re.DOTALL
            )

            download_targets = []
            for fid, name, size, url_val in file_pattern.findall(response or ''):
                # Sanitize filename: remove any XML-like tags that might have leaked in.
                name = re.sub(r'<[^>]+>', '', name).strip()
                # Remove any leading/trailing XML fragments.
                name = re.sub(r'^[<>/]+', '', name)
                name = re.sub(r'[<>/]+$', '', name)
                size = int(size)

                ext = os.path.splitext(name)[1].lstrip('.').lower()
                if self.exts and ext not in [e.lower() for e in self.exts]:
                    continue
                if self.args.max_limit and size > int(self.args.max_limit):
                    continue

                if not url_val:
                    # No direct download URL for this entry.
                    continue

                final_url = html.unescape(url_val)

                # Sanitize filename for filesystem safety (remove invalid chars for macOS/Windows/Linux).
                safe_name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name)
                # Remove any leading dots or spaces that could cause issues.
                safe_name = safe_name.lstrip('. ')

                dest_dir = destination_folder
                if self.args.structure and path_prefix:
                    # Sanitize path_prefix to ensure it's a valid directory name.
                    path_prefix = re.sub(r'[<>:"|?*\x00-\x1f]', '_', path_prefix)
                    path_prefix = path_prefix.strip('. ')
                    dest_dir = os.path.join(destination_folder, path_prefix)

                full_path = os.path.join(dest_dir, safe_name)

                download_targets.append({
                    'name': name,
                    'size': size,
                    'url': final_url,
                    'destination': full_path
                })

            for target in download_targets:
                self._download_binary(target)

        # 3. Handle Recursion
        if self.args.recursive:
            self.log("  Recursing into given URLs...")
            for url in urls:
                resp = self._request(url, method='GET')

                folder_list_match = re.search(r'<div id="foldersList">(.*?)</div>', resp or '', re.DOTALL)

                if folder_list_match:
                    content = folder_list_match.group(1)
                    sub_matches = re.findall(r'href="(.*?)"', content)

                    # Preserve scheme from the parent URL (supports both http and https),
                    # matching the updated PHP behavior.
                    parsed_parent = urlparse(url)
                    parent_scheme = parsed_parent.scheme if parsed_parent.scheme in ('http', 'https') else 'https'

                    sub_urls = []
                    for m in sub_matches:
                        # Build absolute URL.
                        if m.startswith('http://') or m.startswith('https://'):
                            candidate = m
                        else:
                            candidate = f'{parent_scheme}://chomikuj.pl{m}'

                        # Only recurse into folder URLs (no file extension in last path segment).
                        parsed_child = urlparse(candidate)
                        leaf = os.path.basename(parsed_child.path)
                        if '.' in leaf:
                            # Looks like a file, not a folder – skip to avoid re-downloading files.
                            continue

                        sub_urls.append(candidate)

                    if sub_urls:
                        self.download_files(sub_urls, destination_folder)

    def _download_binary(self, file_data):
        """Helper to handle the actual curl/file write logic."""
        url = file_data['url']
        dest = file_data['destination']
        size = file_data['size']
        name = file_data.get('name', os.path.basename(dest))

        callback = getattr(self.args, 'progress_callback', None)
        if callback:
            try:
                callback(name, 'downloading', 'Pobieranie...', 0)
            except Exception:
                pass

        url_display = (url[:20] + '...' + url[-60:]) if len(url) > 80 else url
        self.log(f"Downloading URL \"{url_display}\" ({size} bytes). ", error=False)

        dest_dir = os.path.dirname(dest)
        if not os.path.exists(dest_dir) and self.args.structure:
            os.makedirs(dest_dir, exist_ok=True)
        elif not os.path.exists(dest_dir):
             os.makedirs(dest_dir, exist_ok=True)

        if os.path.exists(dest):
            if self.args.structure and not self.args.overwrite:
                self.log("Already downloaded, skipping.")
                return
            
            if self.args.overwrite:
                self.log("Will Overwrite. ", error=False)
                os.remove(dest)
            else:
                base, ext = os.path.splitext(dest)
                counter = 2
                new_dest = dest
                
                match = re.search(r'\((\d+)\)$', base)
                if match:
                    counter = int(match.group(1)) + 1
                    base = base[:match.start()]
                
                while os.path.exists(new_dest):
                    new_dest = f"{base}({counter}){ext}"
                    counter += 1
                dest = new_dest
                file_data['destination'] = dest

        part_file = dest + '.part'
        headers = {}
        mode = 'wb'
        existing_size = 0
        if os.path.exists(part_file):
            existing_size = os.path.getsize(part_file)
            if existing_size < size:
                self.log(f"Resuming part {existing_size} - {size}... ", error=False)
                headers['Range'] = f"bytes={existing_size}-"
                mode = 'ab'
            else:
                self.log("Already downloaded (part complete), skipping.")
                os.rename(part_file, dest)
                return
        else:
            self.log("Downloading... ", error=False)

        try:
            with self.session.get(url, headers=headers, stream=True) as r:
                if r.status_code == 404:
                    self.log("ERROR: 404 Not Found.")
                    if os.path.exists(part_file):
                        os.remove(part_file)
                    if callback:
                        try:
                            callback(name, 'error', '404 Not Found', None)
                        except Exception:
                            pass
                    return
                total = int(r.headers.get('content-length', 0)) or size
                written = existing_size if mode == 'ab' else 0
                with open(part_file, mode) as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            if callback and total > 0:
                                pct = min(100, int(100 * written / total))
                                try:
                                    callback(name, 'downloading', f'Pobieranie... {pct}%', pct)
                                except Exception:
                                    pass
            os.rename(part_file, dest)
            self.log("Done.")
            if callback:
                try:
                    callback(name, 'success', 'Pobrano pomyślnie', 100)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"ERROR: {e}", error=True)
            if callback:
                try:
                    callback(name, 'error', str(e), None)
                except Exception:
                    pass


    def _request(self, url, method='POST', data=None, headers=None):
        if headers is None:
            headers = {}
        
        try:
            if method == 'POST':
                resp = self.session.post(url, data=data, headers=headers)
            else:
                resp = self.session.get(url, headers=headers)
            
            resp_text = resp.text
            seq_match = re.search(r'<a:messageSequence><stamp>(\d+)</stamp>', resp_text)
            if seq_match:
                self.stamp = int(seq_match.group(1)) + 1000
            
            return resp_text
        except Exception as e:
            self.log(f"Request Error: {e}", error=True)
            return None

def main():
    parser = argparse.ArgumentParser(description="Chomikuj Downloader (Python Port)")
    
    # Required args
    parser.add_argument('destination', nargs='?', default='./', help='Destination folder')
    parser.add_argument('--user', '-u', required=True, help='Chomikuj user name')
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--password', '-p', help='User password')
    group.add_argument('--hash', help='MD5 password hash')
    
    parser.add_argument('--url', required=True, help='URL to download from (https://chomikuj.pl/...)')

    # Optional args
    parser.add_argument('--recursive', '-r', action='store_true', help='Download subdirectories')
    parser.add_argument('--structure', '-s', action='store_true', help='Create folder structure')
    parser.add_argument('--overwrite', '-o', action='store_true', help='Overwrite existing files')
    parser.add_argument('--noprogress', '-n', action='store_true', help='Do not print progress')
    parser.add_argument('--ext', help='Comma separated extensions (e.g. "ttf,otf")')
    parser.add_argument('--max-limit', type=int, help='Max file size in bytes')

    args = parser.parse_args()

    downloader = ChomikDownloader(args)
    downloader.download_files([args.url], args.destination)

if __name__ == '__main__':
    main()