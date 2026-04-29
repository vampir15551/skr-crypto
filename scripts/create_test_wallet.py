"""
Generate a TRON test wallet for Nile testnet e2e tests.

Usage:
    python scripts/create_test_wallet.py

This creates a random keypair and prints:
  - Private key (hex)
  - Public address (T...)
  - Instructions to fund it from Nile faucet
"""
from tronpy.keys import PrivateKey


def main():
    priv = PrivateKey.random()
    address = priv.public_key.to_base58check_address()
    hex_key = priv.hex()

    print()
    print("=" * 60)
    print("  TRON Nile Testnet Wallet")
    print("=" * 60)
    print()
    print(f"  Private key : {hex_key}")
    print(f"  Address     : {address}")
    print()
    print("-" * 60)
    print("  NEXT STEPS:")
    print("-" * 60)
    print()
    print("  1. Get test TRX (for fees):")
    print(f"     https://nileex.io/join/getJoinPage")
    print(f"     -> Paste address: {address}")
    print(f"     -> Request 1000 TRX")
    print()
    print("  2. Get test USDT:")
    print("     The Nile USDT contract is TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf")
    print("     You can send test USDT from nileex.io or interact with the contract")
    print()
    print("  3. Create a second wallet for the recipient:")
    print("     Run this script again, or use any Nile address")
    print()
    print("  4. Run e2e tests:")
    print(f"     TRON_E2E=1 \\")
    print(f"     E2E_PRIVATE_KEY={hex_key} \\")
    print(f"     E2E_TO_ADDRESS=<recipient-address> \\")
    print(f"     python -m pytest tests/test_e2e_testnet.py -v -s")
    print()
    print("  WARNING: This is a TEST wallet. Never use it on mainnet.")
    print("  WARNING: Never commit the private key to git.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
