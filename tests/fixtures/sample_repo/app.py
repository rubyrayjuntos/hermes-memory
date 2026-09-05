"""Sample application entry point."""
from utils.helpers import format_name


def main():
    name = format_name("ada", "lovelace")
    print(f"hello {name}")

if __name__ == "__main__":
    main()
