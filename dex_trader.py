"""
DEX Trading Module for Uniswap V3

Handles swaps on decentralized exchanges using web3.py
"""

import json
import time
import logging
from typing import Optional, Tuple
from decimal import Decimal

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

import config_dex as config


# Uniswap V3 SwapRouter ABI (minimal)
SWAP_ROUTER_ABI = json.loads('''[
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"}
                ],
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]''')

# ERC20 ABI (minimal)
ERC20_ABI = json.loads('''[
    {"constant": true, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": true, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": false, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": true, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
]''')

# Quoter ABI
QUOTER_ABI = json.loads('''[
    {
        "inputs": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"}
        ],
        "name": "quoteExactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]''')


def dex_live_actions_allowed() -> bool:
    """Require DEX-specific opt-in, valid network, and signing material."""
    return bool(
        not getattr(config, "DRY_RUN", True)
        and getattr(config, "DEX_LIVE_TRADING_OPT_IN", False)
        and getattr(config, "DEX_LIVE_RUNTIME_OPT_IN", False)
        and getattr(config, "DEX_NETWORK_VALID", False)
        and getattr(config, "WALLET_PRIVATE_KEY", "")
    )


class DEXTrader:
    """Handles DEX trading via Uniswap V3."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.network = config.NETWORK
        
        # Initialize Web3
        rpc_url = config.RPC_URLS.get(self.network)
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        # Add POA middleware for L2s
        if self.network in ["polygon", "arbitrum", "optimism", "base"]:
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        
        # Load account
        if config.WALLET_PRIVATE_KEY:
            self.account = Account.from_key(config.WALLET_PRIVATE_KEY)
            self.wallet_address = self.account.address
        else:
            self.account = None
            self.wallet_address = None
        
        # Token addresses
        self.weth_address = Web3.to_checksum_address(
            config.TOKENS[self.network]["WETH"]
        )
        self.quote_address = Web3.to_checksum_address(
            config.TOKENS[self.network][config.QUOTE_TOKEN]
        )
        
        # Contracts
        self.router_address = Web3.to_checksum_address(
            config.UNISWAP_ROUTER[self.network]
        )
        self.router = self.w3.eth.contract(
            address=self.router_address,
            abi=SWAP_ROUTER_ABI
        )
        
        self.quoter_address = Web3.to_checksum_address(
            config.UNISWAP_QUOTER[self.network]
        )
        self.quoter = self.w3.eth.contract(
            address=self.quoter_address,
            abi=QUOTER_ABI
        )
        
        # Token contracts
        self.weth = self.w3.eth.contract(address=self.weth_address, abi=ERC20_ABI)
        self.quote_token = self.w3.eth.contract(address=self.quote_address, abi=ERC20_ABI)
        
        self.logger.info(f"DEX Trader initialized on {self.network.upper()}")
        self.logger.info(f"Router: {self.router_address}")
        if self.wallet_address:
            self.logger.info(f"Wallet: {self.wallet_address}")
    
    def is_connected(self) -> bool:
        """Check if connected to the network."""
        return self.w3.is_connected()
    
    def get_eth_price(self) -> Optional[float]:
        """Get current ETH price in quote token."""
        try:
            # Quote 1 ETH -> USDC
            amount_in = Web3.to_wei(1, 'ether')
            
            amount_out = self.quoter.functions.quoteExactInputSingle(
                self.weth_address,
                self.quote_address,
                config.POOL_FEE,
                amount_in,
                0
            ).call()
            
            # USDC has 6 decimals
            decimals = self.quote_token.functions.decimals().call()
            price = amount_out / (10 ** decimals)
            
            return price
            
        except Exception as e:
            self.logger.error(f"Error getting ETH price: {e}")
            return None
    
    def get_balances(self) -> dict:
        """Get wallet balances."""
        if not self.wallet_address:
            return {"ETH": 0, config.QUOTE_TOKEN: 0}
        
        try:
            # Native ETH balance
            eth_balance = self.w3.eth.get_balance(self.wallet_address)
            eth_balance = Web3.from_wei(eth_balance, 'ether')
            
            # WETH balance
            weth_balance = self.weth.functions.balanceOf(self.wallet_address).call()
            weth_balance = Web3.from_wei(weth_balance, 'ether')
            
            # Quote token balance
            quote_decimals = self.quote_token.functions.decimals().call()
            quote_balance = self.quote_token.functions.balanceOf(self.wallet_address).call()
            quote_balance = quote_balance / (10 ** quote_decimals)
            
            return {
                "ETH": float(eth_balance),
                "WETH": float(weth_balance),
                config.QUOTE_TOKEN: float(quote_balance)
            }
            
        except Exception as e:
            self.logger.error(f"Error getting balances: {e}")
            return {"ETH": 0, "WETH": 0, config.QUOTE_TOKEN: 0}
    
    def _get_gas_params(self) -> dict:
        """Get EIP-1559 gas parameters with buffer for price fluctuations."""
        try:
            # Get latest block for base fee
            latest_block = self.w3.eth.get_block('latest')
            base_fee = latest_block.get('baseFeePerGas', self.w3.eth.gas_price)
            
            # Add 25% buffer to base fee to handle fluctuations
            max_fee = int(base_fee * 1.25)
            
            # Priority fee (tip) - 0.1 gwei is usually enough for L2s
            priority_fee = Web3.to_wei(0.1, 'gwei')
            
            # Ensure max_fee is at least base_fee + priority_fee
            if max_fee < base_fee + priority_fee:
                max_fee = base_fee + priority_fee + Web3.to_wei(0.01, 'gwei')
            
            return {
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee
            }
        except Exception as e:
            # Fallback to legacy gas price with buffer
            self.logger.warning(f"EIP-1559 gas estimation failed, using legacy: {e}")
            gas_price = int(self.w3.eth.gas_price * 1.25)
            return {'gasPrice': gas_price}
    
    def _approve_token(self, token_contract, spender: str, amount: int) -> bool:
        """Approve token spending only after both explicit live opt-ins."""
        if not dex_live_actions_allowed():
            self.logger.error("Token approval blocked: DEX live opt-ins are not satisfied")
            return False
        try:
            # Check current allowance
            allowance = token_contract.functions.allowance(
                self.wallet_address,
                spender
            ).call()
            
            if allowance >= amount:
                self.logger.info("Sufficient allowance already exists")
                return True
            
            # Build approval transaction with EIP-1559 gas
            nonce = self.w3.eth.get_transaction_count(self.wallet_address)
            gas_params = self._get_gas_params()
            
            tx_params = {
                'from': self.wallet_address,
                'gas': 100000,
                'nonce': nonce,
                'chainId': config.CHAIN_IDS[self.network],
                **gas_params  # EIP-1559 or legacy gas params
            }
            
            approve_tx = token_contract.functions.approve(
                spender,
                amount
            ).build_transaction(tx_params)
            
            # Sign and send
            signed = self.account.sign_transaction(approve_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            
            self.logger.info(f"Approval tx sent: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                self.logger.info("Approval confirmed")
                return True
            else:
                self.logger.error("Approval failed")
                return False
                
        except Exception as e:
            self.logger.error(f"Approval error: {e}")
            return False
    
    def buy_eth(self, usdc_amount: float) -> Tuple[bool, Optional[str]]:
        """
        Buy ETH with USDC on Uniswap.
        
        Args:
            usdc_amount: Amount of USDC to spend
            
        Returns:
            (success, tx_hash)
        """
        if not dex_live_actions_allowed():
            self.logger.info(f"[DRY RUN] Would buy ETH with {usdc_amount} {config.QUOTE_TOKEN}")
            return True, "dry_run_tx_hash"
        
        if not self.account:
            self.logger.error("No wallet configured")
            return False, None
        
        try:
            # Convert to wei (USDC has 6 decimals)
            decimals = self.quote_token.functions.decimals().call()
            amount_in = int(usdc_amount * (10 ** decimals))
            
            # Get quote for minimum output
            expected_out = self.quoter.functions.quoteExactInputSingle(
                self.quote_address,
                self.weth_address,
                config.POOL_FEE,
                amount_in,
                0
            ).call()
            
            # Apply slippage
            min_out = int(expected_out * (1 - config.SLIPPAGE_PERCENT / 100))
            
            # Approve USDC spending
            if not self._approve_token(self.quote_token, self.router_address, amount_in):
                return False, None
            
            # Build swap transaction
            deadline = int(time.time()) + 300  # 5 minutes
            
            swap_params = {
                'tokenIn': self.quote_address,
                'tokenOut': self.weth_address,
                'fee': config.POOL_FEE,
                'recipient': self.wallet_address,
                'deadline': deadline,
                'amountIn': amount_in,
                'amountOutMinimum': min_out,
                'sqrtPriceLimitX96': 0
            }
            
            nonce = self.w3.eth.get_transaction_count(self.wallet_address)
            gas_params = self._get_gas_params()
            
            tx_params = {
                'from': self.wallet_address,
                'gas': 300000,
                'nonce': nonce,
                'chainId': config.CHAIN_IDS[self.network],
                'value': 0,
                **gas_params  # EIP-1559 or legacy gas params
            }
            
            swap_tx = self.router.functions.exactInputSingle(swap_params).build_transaction(tx_params)
            
            # Sign and send
            signed = self.account.sign_transaction(swap_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            
            self.logger.info(f"🟢 BUY tx sent: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                self.logger.info(f"✅ BUY confirmed! Gas used: {receipt['gasUsed']}")
                return True, tx_hash.hex()
            else:
                self.logger.error("❌ BUY transaction failed")
                return False, tx_hash.hex()
                
        except Exception as e:
            self.logger.error(f"Buy error: {e}")
            return False, None
    
    def sell_eth(self, eth_amount: float) -> Tuple[bool, Optional[str]]:
        """
        Sell ETH (WETH) for USDC on Uniswap.
        
        Args:
            eth_amount: Amount of ETH to sell
            
        Returns:
            (success, tx_hash)
        """
        if not dex_live_actions_allowed():
            self.logger.info(f"[DRY RUN] Would sell {eth_amount} ETH")
            return True, "dry_run_tx_hash"
        
        if not self.account:
            self.logger.error("No wallet configured")
            return False, None
        
        try:
            amount_in = Web3.to_wei(eth_amount, 'ether')
            
            # Get quote for minimum output
            expected_out = self.quoter.functions.quoteExactInputSingle(
                self.weth_address,
                self.quote_address,
                config.POOL_FEE,
                amount_in,
                0
            ).call()
            
            # Apply slippage
            min_out = int(expected_out * (1 - config.SLIPPAGE_PERCENT / 100))
            
            # Approve WETH spending
            if not self._approve_token(self.weth, self.router_address, amount_in):
                return False, None
            
            # Build swap transaction
            deadline = int(time.time()) + 300
            
            swap_params = {
                'tokenIn': self.weth_address,
                'tokenOut': self.quote_address,
                'fee': config.POOL_FEE,
                'recipient': self.wallet_address,
                'deadline': deadline,
                'amountIn': amount_in,
                'amountOutMinimum': min_out,
                'sqrtPriceLimitX96': 0
            }
            
            nonce = self.w3.eth.get_transaction_count(self.wallet_address)
            gas_params = self._get_gas_params()
            
            tx_params = {
                'from': self.wallet_address,
                'gas': 300000,
                'nonce': nonce,
                'chainId': config.CHAIN_IDS[self.network],
                'value': 0,
                **gas_params  # EIP-1559 or legacy gas params
            }
            
            swap_tx = self.router.functions.exactInputSingle(swap_params).build_transaction(tx_params)
            
            # Sign and send
            signed = self.account.sign_transaction(swap_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            
            self.logger.info(f"🔴 SELL tx sent: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                self.logger.info(f"✅ SELL confirmed! Gas used: {receipt['gasUsed']}")
                return True, tx_hash.hex()
            else:
                self.logger.error("❌ SELL transaction failed")
                return False, tx_hash.hex()
                
        except Exception as e:
            self.logger.error(f"Sell error: {e}")
            return False, None


if __name__ == "__main__":
    # Test DEX connection
    logging.basicConfig(level=logging.INFO)
    
    trader = DEXTrader()
    print(f"\nConnected: {trader.is_connected()}")
    
    price = trader.get_eth_price()
    if price:
        print(f"ETH Price: ${price:,.2f}")
    
    balances = trader.get_balances()
    print(f"Balances: {balances}")
