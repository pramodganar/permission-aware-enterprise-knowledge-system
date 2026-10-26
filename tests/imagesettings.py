"""Every environment variable the images may hold, and why it's there.

An image's settings can be read by anyone who has the image, so a secret
put there under any name is given away. Looking for names that sound
secret misses one called something else. So this is an allow-list instead:
a variable not named here fails the tests, and adding one means deciding
it holds nothing secret.
"""

# Set by the Dockerfile.
OURS = {
    "PYTHONDONTWRITEBYTECODE": "no .pyc files written next to the code",
    "PYTHONUNBUFFERED": "log lines leave at once, not when a buffer fills",
    "PIP_NO_CACHE_DIR": "no download cache left in the image",
    "PIP_DISABLE_PIP_VERSION_CHECK": "pip doesn't look for a newer pip",
    "PATH": "the virtual environment's programs first",
    "HF_HOME": "where the embedding model is kept in the image",
    "HF_HUB_OFFLINE": "the model library never reaches the network",
    "APP_EMBEDDING_OFFLINE": "the service loads the model from the image only",
    "APP_DATABASE_URL": "the database on the volume, not next to the code",
    "APP_INDEX_DIRECTORY": "the index on the volume, not next to the code",
    "UI_API_URL": "the API's name on the network the two containers share",
}

# Set by the official Python image the two are built on. All public.
BASE = {
    "LANG": "the locale",
    "GPG_KEY": "the ID of the public key Python's release is signed with",
    "PYTHON_VERSION": "the Python version",
    "PYTHON_SHA256": "the checksum of the Python source that was built",
}

ALLOWED = OURS | BASE
