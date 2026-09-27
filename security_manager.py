import keyring
import getpass
import logging
import os

SERVICE_NAME = "hyperliquid_flush_bot"

class SecurityManager:
    """
    Manages secure access to private keys using the system keyring.
    Falls back to environment variables if keyring is not set.
    """
    @staticmethod
    def get_key(key_name: str) -> str:
        """
        Retrieve a key from the system keyring.
        If not found, try getting it from environment variables.
        """
        try:
            # Try keyring first
            secret = keyring.get_password(SERVICE_NAME, key_name)
            if secret:
                return secret
        except Exception as e:
            logging.warning(f"Keyring access failed: {e}. Falling back to env vars.")
        
        # Fallback to env var
        return os.getenv(key_name, "")

    @staticmethod
    def set_key(key_name: str, secret: str):
        """Securely store a key in the system keyring."""
        try:
            keyring.set_password(SERVICE_NAME, key_name, secret)
            print(f"✅ Securely stored {key_name} in system keychain.")
        except Exception as e:
            print(f"❌ Failed to store key in keychain: {e}")

def setup_keys_interactive():
    """Interactive script to help users set their keys."""
    print("🔐 Security Setup: Hyperliquid Flush Bot")
    print("This will securely store your private keys in your OS keychain.")
    print("Keys are NOT stored in plain text files.")
    print("-" * 50)
    
    # Hyperliquid Key
    hl_key = getpass.getpass("Enter Hyperliquid Private Key (leave empty to skip): ").strip()
    if hl_key:
        if hl_key.startswith("0x"):
            hl_key = hl_key[2:]  # Store without prefix for consistency
        SecurityManager.set_key("HYPERLIQUID_PRIVATE_KEY", hl_key)
        
    # Hyperliquid Address
    hl_addr = input("Enter Hyperliquid Wallet Address (leave empty to skip): ").strip()
    if hl_addr:
        SecurityManager.set_key("HYPERLIQUID_ADDRESS", hl_addr)
        
    print("-" * 50)
    print("✅ Setup complete. You can now run the bot without exporting env vars.")

if __name__ == "__main__":
    setup_keys_interactive()
