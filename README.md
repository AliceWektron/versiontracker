# VersionTracker

An advanced application version tracker for macOS that efficiently retrieves the versions of installed applications on your system and compares them with the latest releases available online. This tool simplifies the process of identifying outdated applications, offering a seamless solution for maintaining your software up to date.

## Prerequisites

Before starting, ensure that you have the following installed:

- [pyenv](https://github.com/pyenv/pyenv) for managing Python versions
- [Nuitka](https://github.com/Nuitka/Nuitka) for compiling Python scripts
  
## Setup Instructions

### Install Python with shared libraries
```bash
env PYTHON_CONFIGURE_OPTS="--enable-shared" pyenv install {version}
```
### Create and activate the virtual environment

```bash
python -m venv venv
source venv/bin/activate
```
### Install dependencies:
```bash
pip install -r requirements.txt
```

### 4. Compile the script with Nuitka
```bash
LDFLAGS="-L$HOME/.pyenv/versions/3.12.7/lib" \
CPPFLAGS="-I/opt/homebrew/opt/gettext/include" \
nuitka --macos-target-arch=arm64 \
       --onefile \
       --follow-imports \
       --static-libpython=no \
       main.py
```