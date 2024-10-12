import os
import sys
import plistlib
import re
import aiohttp
import asyncio
import shutil
import json
import concurrent.futures
import subprocess
from bs4 import BeautifulSoup
from packaging import version
import toml

# Additional imports for implementing retry logic in HTTP requests
from aiohttp import ClientSession, TCPConnector
from aiohttp_retry import RetryClient, ExponentialRetry

# ANSI escape codes for colored terminal output to enhance readability
CRIMSON = "\033[91m"
CERULEAN_BLUE = "\033[94m"
NEON_GREEN = "\033[92m"
VIOLET = "\033[35m"
RESET = "\033[0m"

# URL from which to fetch the config.toml file
CONFIG_URL = "https://raw.githubusercontent.com/AliceWektron/versiontracker/refs/heads/main/config.toml"

# Semaphore limit for concurrent HTTP requests
SEM_LIMIT = 20  # Adjust based on your system/network capabilities

# In-memory storage for installed application versions
installed_versions = {}

def print_dashed_line():
    """
    Print a dashed line spanning the entire width of the terminal.
    Useful for separating sections in the output.
    """
    terminal_width = shutil.get_terminal_size().columns
    print("-" * terminal_width)

def parse_info_plist(app_path):
    """
    Parse the Info.plist file of a macOS application to extract key information.

    Args:
        app_path (str): Path to the .app directory.

    Returns:
        tuple: Executable name, bundle identifier, bundle version, and short version string.
    """
    info_plist_path = os.path.join(app_path, "Contents", "Info.plist")
    try:
        with open(info_plist_path, "rb") as plist_file:
            plist_data = plistlib.load(plist_file)
            return (
                plist_data.get("CFBundleExecutable", ""),
                plist_data.get("CFBundleIdentifier", ""),
                plist_data.get("CFBundleVersion", ""),
                plist_data.get("CFBundleShortVersionString", "")
            )
    except (FileNotFoundError, plistlib.InvalidFileException):
        # Return empty strings if the Info.plist is missing or invalid
        return "", "", "", ""

def find_app_folders(root_dir):
    """
    Recursively search for all .app directories within a specified root directory.

    Args:
        root_dir (str): The directory to begin searching from.

    Returns:
        list: A list of paths to .app directories found.
    """
    app_folders = []
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.endswith('.app'):
                        app_folders.append(entry.path)
                    else:
                        # Recursively search subdirectories
                        app_folders.extend(find_app_folders(entry.path))
    except PermissionError:
        # Skip directories that cannot be accessed due to permission issues
        pass
    return app_folders

def normalize_version(version_string):
    """
    Normalize a version string by retaining only digits and dots.

    Args:
        version_string (str): The original version string.

    Returns:
        str: A cleaned and standardized version string.
    """
    # Retain only digits and dots, replacing other characters with dots
    normalized_version = ''.join(c if c.isdigit() or c == '.' else '.' for c in version_string)
    # Replace multiple consecutive dots with a single dot
    normalized_version = re.sub(r'\.+', '.', normalized_version)
    # Remove leading and trailing dots
    normalized_version = normalized_version.strip('.')
    return normalized_version

def compare_versions(installed_version, latest_version):
    """
    Compare two version strings to determine their relationship.

    Args:
        installed_version (str): The currently installed version.
        latest_version (str): The latest available version.

    Returns:
        str: One of "update_available", "up_to_date", "versions_equal", or "unknown".
    """
    try:
        installed_version_obj = version.parse(normalize_version(installed_version))
        latest_version_obj = version.parse(normalize_version(latest_version))
    except version.InvalidVersion:
        return "unknown"

    if installed_version_obj < latest_version_obj:
        return "update_available"
    elif installed_version_obj > latest_version_obj:
        return "up_to_date"
    else:
        return "versions_equal"

def get_mdls_version(app_path):
    """
    Retrieve the application version using the macOS `mdls` command.

    Args:
        app_path (str): Path to the .app directory.

    Returns:
        str: The version string retrieved from metadata, or an empty string if unsuccessful.
    """
    try:
        mdls_output = subprocess.check_output(
            ["mdls", "-name", "kMDItemVersion", app_path],
            stderr=subprocess.DEVNULL
        )
        version_str = mdls_output.decode("utf-8").strip().replace('kMDItemVersion = "', '').replace('"', '')
        return version_str
    except subprocess.CalledProcessError:
        # Return empty string if the mdls command fails
        return ""

async def fetch_config(session, url):
    """
    Asynchronously fetch and parse the config.toml file from a given URL.

    Args:
        session (aiohttp.ClientSession): The HTTP session for making requests.
        url (str): The URL to fetch the config.toml from.

    Returns:
        dict: Parsed configuration data.

    Raises:
        RuntimeError: If the config file cannot be fetched or parsed.
    """
    try:
        async with session.get(url, timeout=10) as response:
            response.raise_for_status()
            config_text = await response.text()
            config_data = toml.loads(config_text)
            return config_data
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"{CRIMSON}Failed to fetch config from URL: {e}{RESET}")
        raise RuntimeError("Config fetch failed.")
    except toml.TomlDecodeError as e:
        print(f"{CRIMSON}Error parsing TOML config from URL: {e}{RESET}")
        raise RuntimeError("Config parsing failed.")

async def scrape_latest_version(session, url, label):
    """
    Scrape the latest version of an application from the MacUpdater website.

    Args:
        session (aiohttp.ClientSession): The HTTP session for making requests.
        url (str): The URL of the application's update page.
        label (str): The label preceding the version string in the HTML.

    Returns:
        str: The latest version string if found, else an empty string.
    """
    try:
        async with session.get(url, timeout=10) as response:
            response.raise_for_status()
            text = await response.text()
            soup = BeautifulSoup(text, "html.parser")
            for td in soup.find_all("td"):
                if td.text.strip() == label:
                    next_td = td.find_next_sibling("td")
                    if next_td:
                        return next_td.text.strip()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        # Return empty string if the request fails or times out
        return ""
    return ""

async def fetch_latest_version_for_app(app_entry, session, semaphore, identifier_mappings):
    """
    Asynchronously fetch the latest version for a single application.

    Args:
        app_entry (list): A list containing app details [executable, identifier, installed_version].
        session (aiohttp.ClientSession): The HTTP session for making requests.
        semaphore (asyncio.Semaphore): Semaphore to limit concurrent requests.
        identifier_mappings (dict): Mappings to correct bundle identifiers if necessary.

    Returns:
        list: Updated app_entry with the latest version appended.
    """
    bundle_executable, bundle_identifier, installed_version = app_entry

    # Apply bundle identifier mapping if available
    corrected_bundle_identifier = identifier_mappings.get(bundle_identifier, bundle_identifier)

    # Construct the MacUpdater URL for the application
    macupdater_url = f"https://macupdater.net/app_updates/appinfo/{corrected_bundle_identifier}/index.html"

    # Limit the number of concurrent requests using semaphore
    async with semaphore:
        latest_version = await scrape_latest_version(session, macupdater_url, "Version String:")

    # Append the latest version to the app_entry
    app_entry.append(latest_version)

    # Display retrieval status with colored output
    if latest_version:
        print(f"{CERULEAN_BLUE}Retrieving {bundle_executable}, {bundle_identifier} {NEON_GREEN}[{latest_version}]{RESET}")
    else:
        print(f"{CERULEAN_BLUE}Retrieving {bundle_executable}, {bundle_identifier} {CRIMSON}*Unavailable*{RESET}")

    return app_entry

async def fetch_latest_versions(app_data, session, semaphore, identifier_mappings):
    """
    Asynchronously fetch the latest versions for all applications.

    Args:
        app_data (list): A list of application data entries.
        session (aiohttp.ClientSession): The HTTP session for making requests.
        semaphore (asyncio.Semaphore): Semaphore to limit concurrent requests.
        identifier_mappings (dict): Mappings to correct bundle identifiers if necessary.

    Returns:
        list: A list of updated application data entries with latest versions.
    """
    tasks = [
        fetch_latest_version_for_app(app_entry, session, semaphore, identifier_mappings)
        for app_entry in app_data
    ]
    return await asyncio.gather(*tasks)

def load_all_info_plists(app_folders, ignore_app_names, app_name_mappings):
    """
    Load and parse Info.plist files for all applications concurrently.

    Args:
        app_folders (list): A list of paths to .app directories.
        ignore_app_names (list): List of application executable names to ignore.
        app_name_mappings (dict): Mappings to rename executables if necessary.

    Returns:
        list: A list of application data entries [executable, identifier, installed_version].
    """
    app_data = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Submit tasks to the thread pool for retrieving mdls versions
        futures = {executor.submit(get_mdls_version, app_folder): app_folder for app_folder in app_folders}
        for future in concurrent.futures.as_completed(futures):
            app_folder = futures[future]
            mdls_version = future.result()
            bundle_executable, bundle_identifier, bundle_version, bundle_short_version = parse_info_plist(app_folder)

            # Skip applications with missing essential information
            if not bundle_executable or not bundle_identifier:
                continue

            # Exclude applications specified in the ignore list
            if bundle_executable in ignore_app_names:
                continue

            # Apply executable name mapping if applicable
            bundle_executable = app_name_mappings.get(bundle_executable, bundle_executable)

            # Determine the build version by comparing bundle and mdls versions
            build_version = ""
            if bundle_version and bundle_version != mdls_version:
                build_version = bundle_version
            elif bundle_short_version and bundle_short_version != mdls_version:
                build_version = bundle_short_version

            # Format the installed version string
            installed_version = f"{mdls_version} ({build_version})" if build_version else mdls_version

            # Store the installed version in the in-memory dictionary
            installed_versions[bundle_identifier] = installed_version

            # Append application data with a placeholder for the latest version
            app_data.append([bundle_executable, bundle_identifier, installed_version])

    return app_data

async def main_async(config_url):
    """
    Main asynchronous function to handle configuration loading, fetching of latest application versions,
    and identifying available updates.

    Args:
        config_url (str): The URL from which to fetch the config.toml file.
    """
    # Initialize retry options for HTTP requests
    retry_options = ExponentialRetry(attempts=3)
    connector = TCPConnector(limit_per_host=SEM_LIMIT)

    # Initialize RetryClient with specified retry options
    async with RetryClient(
        raise_for_status=False,
        client_session=ClientSession(connector=connector),
        retry_options=retry_options
    ) as session:
        # Fetch and parse the configuration from the provided URL
        try:
            config = await fetch_config(session, config_url)
        except RuntimeError:
            sys.exit(1)  # Exit if config cannot be loaded

        # Extract specific configuration sections with default fallbacks
        ignore_app_names = config.get('ignored_apps', {}).get('apps', [])
        app_name_mappings = config.get('app_name_mappings', {})
        identifier_mappings = config.get('identifier_mappings', {})

        # Define the Applications directory to scan
        applications_dir = "/Applications"

        # Discover all .app directories within the Applications folder
        app_folders = find_app_folders(applications_dir)

        # Inform the user that the retrieval process has started
        print(f"{CERULEAN_BLUE}Retrieving installed application versions...{RESET}")

        # Load and parse Info.plist files concurrently to gather installed app data
        app_data = load_all_info_plists(app_folders, ignore_app_names, app_name_mappings)

        # Sort applications alphabetically by their name (case-insensitive)
        app_data.sort(key=lambda x: x[0].lower())

        # Create a semaphore to limit concurrent HTTP requests
        semaphore = asyncio.Semaphore(SEM_LIMIT)

        # Fetch the latest available versions for all applications
        await fetch_latest_versions(app_data, session, semaphore, identifier_mappings)

        # Notify the user that the retrieval process is complete
        print(f"\n{NEON_GREEN}Retrieved installed and latest app versions.{RESET}")

        # Initialize a list to hold applications that have available updates
        updates = []

        # Iterate through each application's data to determine if an update is needed
        for app_entry in app_data:
            if len(app_entry) < 4:
                continue  # Ensure that the latest_version field exists

            app_name, bundle_identifier, installed_version, latest_version = app_entry

            if not installed_version or not latest_version:
                continue  # Skip applications with incomplete version information

            # Skip if the installed version already matches or exceeds the latest version
            if latest_version in installed_version or installed_version.startswith(latest_version):
                continue

            # Compare installed and latest versions to determine update necessity
            comparison_result = compare_versions(installed_version, latest_version)

            if comparison_result == "update_available":
                updates.append(app_entry)
            elif comparison_result == "unknown":
                continue  # Unable to determine version comparison

        # Display the list of applications that have available updates
        if updates:
            print(f"\n{VIOLET}Available Updates:{RESET}\n")
            for app_entry in updates:
                app_name, bundle_identifier, installed_version, latest_version = app_entry
                print(f"Application: {CRIMSON}{app_name}{RESET}")
                print(f"Installed Version: {NEON_GREEN}{installed_version}{RESET}, Latest Version: {NEON_GREEN}{latest_version}{RESET}")
                print_dashed_line()
        else:
            print(f"\n{VIOLET}No Updates Found{RESET}")

def main():
    """
    The main function orchestrates the asynchronous retrieval of configuration,
    fetching of installed and latest application versions, and identifying updates.
    """
    # Run the asynchronous main function with the specified config URL
    asyncio.run(main_async(CONFIG_URL))

if __name__ == "__main__":
    main()
