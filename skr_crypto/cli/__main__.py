"""Allow ``python -m skr_crypto`` as an alternative to the installed
console-script entry point."""
from skr_crypto.cli.main import main

if __name__ == "__main__":
    main()
