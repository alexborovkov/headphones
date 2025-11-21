from collections import defaultdict, namedtuple
import os
import time
import slskd_api
import headphones
from headphones import logger
from datetime import datetime, timedelta

Result = namedtuple('Result', ['title', 'size', 'user', 'provider', 'type', 'matches', 'bandwidth', 'hasFreeUploadSlot', 'queueLength', 'files', 'kind', 'url', 'folder'])

def initialize_soulseek_client():
    try:
        host = headphones.CONFIG.SOULSEEK_API_URL
        api_key = headphones.CONFIG.SOULSEEK_API_KEY
        logger.debug(f"Initializing Soulseek client with host: {host}")

        # Try to initialize with timeout support if available
        try:
            client = slskd_api.SlskdClient(host=host, api_key=api_key, timeout=10)
            logger.debug("Soulseek client initialized successfully with 10s timeout")
        except TypeError:
            # If timeout parameter not supported, try without it
            logger.debug("Timeout parameter not supported, initializing without timeout")
            client = slskd_api.SlskdClient(host=host, api_key=api_key)
            logger.debug("Soulseek client initialized successfully")

        return client
    except Exception as e:
        logger.error(f"Failed to initialize Soulseek client: {e}", exc_info=True)
        raise

    # Search logic, calling search and processing fucntions
def search(artist, album, year, num_tracks, losslessOnly, allow_lossless, user_search_term):
    try:
        logger.debug(f"=== soulseek.search() called ===")
        logger.debug(f"Parameters: artist='{artist}', album='{album}', year={year}, num_tracks={num_tracks}")
        logger.debug(f"Quality settings: losslessOnly={losslessOnly}, allow_lossless={allow_lossless}")
        logger.debug(f"User search term: '{user_search_term}'")

        client = initialize_soulseek_client()

        # override search string with user provided search term if entered
        if user_search_term:
            logger.info(f"Using user-provided search term: '{user_search_term}'")
            artist = user_search_term
            album = ''
            year = ''

        # Stage 1: Search with artist, album, year, and num_tracks
        logger.info(f"[Stage 1] Searching Soulseek using term: {artist} {album} {year}")
        results = execute_search(client, artist, album, year, losslessOnly, allow_lossless)
        processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks)
        logger.debug(f"[Stage 1] Processed {len(processed_results) if processed_results else 0} results")

        if processed_results or user_search_term or album.lower() == artist.lower():
            if processed_results:
                logger.info(f"[Stage 1] SUCCESS - Found {len(processed_results)} matching results")
            else:
                logger.info(f"[Stage 1] No results but stopping (user_search_term={bool(user_search_term)}, same_name={album.lower() == artist.lower()})")
            return processed_results

        logger.debug("[Stage 1] No results meeting criteria, proceeding to Stage 2")

        # Stage 2: If Stage 1 fails, search with artist, album, and num_tracks (excluding year)
        logger.info("[Stage 2] Soulseek search stage 1 did not meet criteria. Retrying without year...")
        results = execute_search(client, artist, album, None, losslessOnly, allow_lossless)
        processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks)
        logger.debug(f"[Stage 2] Processed {len(processed_results) if processed_results else 0} results")

        if processed_results or artist == "Various Artists":
            if processed_results:
                logger.info(f"[Stage 2] SUCCESS - Found {len(processed_results)} matching results")
            else:
                logger.info(f"[Stage 2] No results but stopping (Various Artists)")
            return processed_results

        logger.debug("[Stage 2] No results meeting criteria, proceeding to Stage 3")

        # Stage 3: Final attempt, search only with artist and album
        logger.info("[Stage 3] Soulseek search stage 2 did not meet criteria. Final attempt with only artist and album (ignoring track count).")
        results = execute_search(client, artist, album, None, losslessOnly, allow_lossless)
        processed_results = process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=True)
        logger.debug(f"[Stage 3] Processed {len(processed_results) if processed_results else 0} results")

        if processed_results:
            logger.info(f"[Stage 3] SUCCESS - Found {len(processed_results)} matching results")
        else:
            logger.info("[Stage 3] No matching results found after all stages")

        return processed_results

    except Exception as e:
        logger.error(f"Fatal error in soulseek.search(): {e}", exc_info=True)
        return []

def execute_search(client, artist, album, year, losslessOnly, allow_lossless):
    try:
        search_text = f"{artist} {album}"
        if year:
            search_text += f" {year}"

        if losslessOnly:
            search_text += " flac"
        elif not allow_lossless:
                search_text += " mp3"

        logger.debug(f"Executing search with text: '{search_text}'")

        # Actual search with timeout handling
        logger.debug("Calling client.searches.search_text()...")
        try:
            search_response = client.searches.search_text(searchText=search_text, filterResponses=True)
            logger.debug(f"search_text() returned: {search_response}")
        except Exception as api_error:
            logger.error(f"API call to search_text() failed: {api_error}", exc_info=True)
            logger.error("This usually means the slskd server is not responding or unreachable")
            return []

        search_id = search_response.get('id')
        logger.debug(f"Search initiated with ID: {search_id}")

        # Wait for search completion with timeout (max 60 seconds)
        timeout = 60
        elapsed = 0
        logger.debug("Waiting for search to complete...")
        while True:
            try:
                state = client.searches.state(id=search_id)
                is_complete = state.get('isComplete')
                if is_complete:
                    break
            except Exception as state_error:
                logger.error(f"Error checking search state: {state_error}")
                break

            time.sleep(2)
            elapsed += 2
            if elapsed >= timeout:
                logger.warning(f"Search {search_id} timed out after {timeout} seconds")
                break
            if elapsed % 10 == 0:
                logger.debug(f"Search {search_id} still running ({elapsed}s elapsed)...")

        logger.debug(f"Search {search_id} completed after {elapsed} seconds")

        try:
            responses = client.searches.search_responses(id=search_id)
            logger.debug(f"Retrieved {len(responses) if responses else 0} search responses")
            return responses
        except Exception as response_error:
            logger.error(f"Error retrieving search responses: {response_error}")
            return []

    except Exception as e:
        logger.error(f"Error in execute_search(): {e}", exc_info=True)
        return []

# Processing the search result passed
def process_results(results, losslessOnly, allow_lossless, num_tracks, ignore_track_count=False):
    logger.debug(f"process_results() called with {len(results) if results else 0} raw responses, num_tracks={num_tracks}, ignore_track_count={ignore_track_count}")

    if losslessOnly:
        valid_extensions = {'.flac'}
    elif allow_lossless:
        valid_extensions = {'.mp3', '.flac'}
    else:
        valid_extensions = {'.mp3'}

    logger.debug(f"Valid file extensions: {valid_extensions}")

    albums = defaultdict(lambda: {'files': [], 'user': None, 'hasFreeUploadSlot': None, 'queueLength': None, 'uploadSpeed': None})

    # Extract info from the api response and combine files at album level
    for result in results:
        user = result.get('username')
        hasFreeUploadSlot = result.get('hasFreeUploadSlot')
        queueLength = result.get('queueLength')
        uploadSpeed = result.get('uploadSpeed')

        # Only handle .mp3 and .flac
        for file in result.get('files', []):
            filename = file.get('filename')
            file_extension = os.path.splitext(filename)[1].lower()
            if file_extension in valid_extensions:
                #album_directory = os.path.dirname(filename)
                album_directory = filename.rsplit('\\', 1)[0]
                albums[album_directory]['files'].append(file)

                # Update metadata only once per album_directory
                if albums[album_directory]['user'] is None:
                    albums[album_directory].update({
                        'user': user,
                        'hasFreeUploadSlot': hasFreeUploadSlot,
                        'queueLength': queueLength,
                        'uploadSpeed': uploadSpeed,
                    })

    # Filter albums based on num_tracks, add bunch of useful info to the compiled album
    logger.debug(f"Found {len(albums)} unique album directories after grouping files")
    final_results = []
    for directory, album_data in albums.items():
        file_count = len(album_data['files'])
        if ignore_track_count and file_count > 1 or file_count == num_tracks:
            #album_title = os.path.basename(directory)
            album_title = directory.rsplit('\\', 1)[1]
            total_size = sum(file.get('size', 0) for file in album_data['files'])
            logger.debug(f"Including album '{album_title}' with {file_count} tracks (user: {album_data['user']})")
            final_results.append(Result(
                title=album_title,
                size=int(total_size),
                user=album_data['user'],
                provider="soulseek",
                type="soulseek",
                matches=True,
                bandwidth=album_data['uploadSpeed'],
                hasFreeUploadSlot=album_data['hasFreeUploadSlot'],
                queueLength=album_data['queueLength'],
                files=album_data['files'],
                kind='soulseek',
                url='http://' + album_data['user'] + album_title, # URL is needed in other parts of the program.
                folder=album_title
            ))
        else:
            logger.debug(f"Filtering out album with {file_count} tracks (expected {num_tracks}, ignore_track_count={ignore_track_count})")

    logger.debug(f"process_results() returning {len(final_results)} albums after track count filtering")
    return final_results


def download(user, filelist):
    client = initialize_soulseek_client()
    client.transfers.enqueue(username=user, files=filelist)


def download_completed():
    client = initialize_soulseek_client()
    all_downloads = client.transfers.get_all_downloads(includeRemoved=False)
    album_completion_tracker = {}  # Tracks completion state of each album's songs
    album_errored_tracker = {}  # Tracks albums with errored downloads

    # Anything older than 24 hours will be canceled
    cutoff_time = datetime.now() - timedelta(hours=24)

    # Identify errored and completed albums
    for download in all_downloads:
        directories = download.get('directories', [])
        for directory in directories:
            album_part = directory.get('directory', '').split('\\')[-1]
            files = directory.get('files', [])
            for file_data in files:
                state = file_data.get('state', '')
                requested_at_str = file_data.get('requestedAt', '1900-01-01 00:00:00')
                requested_at = parse_datetime(requested_at_str)

                # Initialize or update album entry in trackers
                if album_part not in album_completion_tracker:
                    album_completion_tracker[album_part] = {'total': 0, 'completed': 0, 'errored': 0}
                if album_part not in album_errored_tracker:
                    album_errored_tracker[album_part] = False

                album_completion_tracker[album_part]['total'] += 1

                if 'Completed, Succeeded' in state:
                    album_completion_tracker[album_part]['completed'] += 1
                elif 'Completed, Errored' in state or requested_at < cutoff_time:
                    album_completion_tracker[album_part]['errored'] += 1
                    album_errored_tracker[album_part] = True  # Mark album as having errored downloads

    # Identify errored albums
    errored_albums = {album for album, errored in album_errored_tracker.items() if errored}

    # Cancel downloads for errored albums
    for download in all_downloads:
        directories = download.get('directories', [])
        for directory in directories:
            album_part = directory.get('directory', '').split('\\')[-1]
            files = directory.get('files', [])
            for file_data in files:
                if album_part in errored_albums:
                    # Extract 'id' and 'username' for each file to cancel the download
                    file_id = file_data.get('id', '')
                    username = file_data.get('username', '')
                    success = client.transfers.cancel_download(username, file_id)
                    if not success:
                        logger.debug(f"Soulseek failed to cancel download for file ID: {file_id}")

    # Clear completed/canceled/errored stuff from client downloads
    try:
        client.transfers.remove_completed_downloads()
    except Exception as e:
        logger.debug(f"Soulseek failed to remove completed downloads: {e}")

    # Identify completed albums
    completed_albums = {album for album, counts in album_completion_tracker.items() if counts['total'] == counts['completed']}

    # Return both completed and errored albums
    return completed_albums, errored_albums


def download_completed_album(username, foldername):
    client = initialize_soulseek_client()
    downloads = client.transfers.get_downloads(username)

    # Anything older than 24 hours will be canceled
    cutoff_time = datetime.now() - timedelta(hours=24)

    total_count = 0
    completed_count = 0
    errored_count = 0
    file_ids = []

    # Identify errored and completed album
    directories = downloads.get('directories', [])
    for directory in directories:
        album_part = directory.get('directory', '').split('\\')[-1]
        if album_part == foldername:
            files = directory.get('files', [])
            for file_data in files:
                state = file_data.get('state', '')
                requested_at_str = file_data.get('requestedAt', '1900-01-01 00:00:00')
                requested_at = parse_datetime(requested_at_str)

                total_count += 1
                file_id = file_data.get('id', '')
                file_ids.append(file_id)

                if 'Completed, Succeeded' in state:
                    completed_count += 1
                elif 'Completed, Errored' in state or requested_at < cutoff_time:
                    errored_count += 1
            break

    completed = True if completed_count == total_count else False
    errored = True if errored_count else False

    # Cancel downloads for errored album
    if errored:
        for file_id in file_ids:
            try:
                success = client.transfers.cancel_download(username, file_id, remove=True)
            except Exception as e:
                logger.debug(f"Soulseek failed to cancel download for folder with file ID: {foldername} {file_id}")

    return completed, errored


def parse_datetime(datetime_string):
    # Parse the datetime api response
    if '.' in datetime_string:
        datetime_string = datetime_string[:datetime_string.index('.')+7]
    return datetime.strptime(datetime_string, '%Y-%m-%dT%H:%M:%S.%f')