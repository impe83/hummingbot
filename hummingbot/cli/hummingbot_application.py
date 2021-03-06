#!/usr/bin/env python

import asyncio
import logging
import argparse
from eth_account.local import LocalAccount
import pandas as pd
import re
from typing import (
    List,
    Dict,
    Optional,
    Tuple
)
from web3 import Web3

from wings.web3_wallet import Web3Wallet
from wings.market_base import MarketBase
from wings.binance_market import BinanceMarket
from wings.radar_relay_market import RadarRelayMarket
from wings.ddex_market import DDEXMarket
from wings.clock import Clock, ClockMode
from wings.ethereum_chain import EthereumChain
from wings.order_book_tracker import OrderBookTrackerDataSourceType
from wings.limit_order import LimitOrder

from hummingbot import init_logging
from hummingbot.cli.ui.keybindings import load_key_bindings
from hummingbot.cli.ui.parser import load_parser, ThrowingArgumentParser
from hummingbot.cli.ui.hummingbot_cli import HummingbotCLI
from hummingbot.cli.ui.completer import load_completer
from hummingbot.cli.utils.symbol_splitter import SymbolSplitter
from hummingbot.cli.utils.wallet_setup import (
    create_and_save_wallet,
    import_and_save_wallet,
    list_wallets,
    unlock_wallet
)
from hummingbot.cli.errors import (
    InvalidCommandError,
    ArgumentParserError
)
from hummingbot.cli.settings import (
    in_memory_config_map,
    global_config_map,
    get_strategy_config_map,
    write_config_to_yml,
    load_required_configs,
    EXCHANGES,
    ConfigVar,
    parse_cvar_value,
    copy_strategy_template,
)
from hummingbot.logger.report_aggregator import ReportAggregator
from hummingbot.strategy.cross_exchange_market_making import (
    CrossExchangeMarketMakingStrategy,
    CrossExchangeMarketPair,
)
from hummingbot.strategy.arbitrage import (
    ArbitrageStrategy,
    ArbitrageMarketPair
)
from hummingbot.cli.settings import get_erc20_token_addresses
from hummingbot.cli.utils.exchange_rate_conversion import ExchangeRateConversion
s_logger = None


def check_web3(ethereum_rpc_url: str) -> bool:
    try:
        w3: Web3 = Web3(Web3.HTTPProvider(ethereum_rpc_url, request_kwargs={"timeout": 2.0}))
        ret = w3.isConnected()
    except Exception:
        ret = False

    if not ret and ethereum_rpc_url.startswith("http://mainnet.infura.io"):
        logging.getLogger().warning("You are connecting to an Infura using an insecure network protocol "
                                    "(\"http\"), which may not be allowed by Infura. Try using \"https://\" instead.")
    return ret


class HummingbotApplication:
    KILL_TIMEOUT = 5.0

    @classmethod
    def logger(cls) -> logging.Logger:
        global s_logger
        if s_logger is None:
            s_logger = logging.getLogger(__name__)
        return s_logger

    def __init__(self):
        self.ev_loop: asyncio.BaseEventLoop = asyncio.get_event_loop()
        self.parser: ThrowingArgumentParser = load_parser(self)
        self.app = HummingbotCLI(
            input_handler=self._handle_command,
            bindings=load_key_bindings(self),
            completer=load_completer(self))

        self.acct: Optional[LocalAccount] = None
        self.markets: Dict = {}
        self.wallet: Optional[Web3Wallet] = None
        self.strategy_task: Optional[asyncio.Task] = None
        self.strategy: Optional[CrossExchangeMarketMakingStrategy] = None
        self.market_pair: Optional[CrossExchangeMarketPair] = None
        self.clock: Optional[Clock] = None

        self.placeholder_mode = False
        self.log_queue_listener: Optional[logging.handlers.QueueListener] = None
        self.reporting_module: Optional[ReportAggregator] = None

    def init_reporting_module(self):
        if not self.reporting_module:
            self.reporting_module = ReportAggregator(
                self,
                report_aggregation_interval=global_config_map["reporting_aggregation_interval"].value,
                log_report_interval=global_config_map["reporting_log_interval"].value)
        self.reporting_module.start()

    def _handle_command(self, raw_command: str):
        raw_command = raw_command.lower().strip()
        try:
            if self.placeholder_mode:
                pass
            else:
                logging.getLogger("hummingbot.command_history").info(raw_command)
                args = self.parser.parse_args(args=raw_command.split())
                kwargs = vars(args)
                if not hasattr(args, "func"):
                    return
                f = args.func
                del kwargs['func']
                f(**kwargs)
        except InvalidCommandError as e:
            self.app.log("Invalid command: %s" % (str(e),))
        except ArgumentParserError as e:
            self.app.log(str(e))
        except NotImplementedError:
            self.app.log("Command not yet implemented. This feature is currently under development.")
        except Exception as e:
            self.logger().error(e, exc_info=True)

    async def _cancel_outstanding_orders(self) -> bool:
        on_chain_cancel_on_exit = global_config_map.get("on_chain_cancel_on_exit").value
        success = True
        self.app.log("Cancelling outstanding orders...")
        for market_name, market in self.markets.items():
            # By default, the bot does not cancel orders on exit on Radar Relay, since all open orders will
            # expire in a short window
            if not on_chain_cancel_on_exit and market_name == "radar_relay":
                continue
            cancellation_results = await market.cancel_all(self.KILL_TIMEOUT)
            uncancelled = list(filter(lambda cr: cr.success is False, cancellation_results))
            if len(uncancelled) > 0:
                success = False
                uncancelled_order_ids = list(map(lambda cr: cr.order_id, uncancelled))
                self.app.log("\nFailed to cancel the following orders on %s:\n%s" % (
                    market_name,
                    '\n'.join(uncancelled_order_ids)
                ))
        if success:
            self.app.log("All outstanding orders cancelled.")
        return success

    async def run(self):
        await self.app.run()

    @property
    def config_complete(self):
        return len(self._get_empty_configs()) == 0

    @staticmethod
    def _get_empty_configs() -> List[str]:
        config_map = load_required_configs()
        return [key for key, config in config_map.items() if config.value is None]

    def get_wallet_balance(self) -> pd.DataFrame:
        return pd.DataFrame(data=list(self.wallet.get_all_balances().items()),
                            columns=["currency", "balance"]).set_index("currency")

    def get_exchange_balance(self, exchange_name: str) -> pd.DataFrame:
        market: MarketBase = self.markets[exchange_name]
        raw_balance: pd.DataFrame = pd.DataFrame(data=list(market.get_all_balances().items()),
                                                 columns=["currency", "balance"]).set_index("currency")
        return raw_balance[raw_balance.balance > 0]

    def config(self, key: str = None):
        self.app.clear_input()
        if key is not None and key not in load_required_configs().keys():
            self.app.log("Invalid config variable %s" % (key,))
            return
        if key is not None:
            keys = [key]
        else:
            keys = self._get_empty_configs()
        asyncio.ensure_future(self._config_loop(keys))

    async def _create_or_import_wallet(self):
        choice = await self.app.prompt(prompt=global_config_map.get("wallet").prompt)
        if choice == "import":
            private_key = await self.app.prompt(prompt="Your wallet private key >>> ", is_password=True)
            password = await self.app.prompt(prompt="A password to protect your wallet key >>> ", is_password=True)

            self.acct = import_and_save_wallet(password, private_key)
            self.app.log("Wallet %s imported into hummingbot" % (self.acct.address,))
        elif choice == "create":
            password = await self.app.prompt(prompt="A password to protect your wallet key >>> ", is_password=True)
            self.acct = create_and_save_wallet(password)
            self.app.log("New wallet %s created" % (self.acct.address,))
        else:
            self.app.log('Invalid choice. Please enter "create" or "import".')
            result = await self._create_or_import_wallet()
            return result
        return self.acct.address

    async def _unlock_wallet(self):
        choice = await self.app.prompt(prompt="Would you like to unlock your previously saved wallet? (y/n) >>> ")
        if choice.lower() == "y":
            wallets = list_wallets()
            self.app.log("Existing wallets:")
            self.list(obj="wallets")
            if len(wallets) == 1:
                public_key = wallets[0]
            else:
                public_key = await self.app.prompt(prompt="Which wallet would you like to import ? >>> ")
            password = await self.app.prompt(prompt="Enter your password >>> ", is_password=True)
            try:
                acct = unlock_wallet(public_key=public_key, password=password)
                self.app.log("Wallet %s unlocked" % (acct.address,))
                self.acct = acct
                return self.acct.address
            except Exception as e:
                self.app.log("Cannot unlock wallet. Please try again.")
                result = await self._unlock_wallet()
                return result
        else:
            value = await self._create_or_import_wallet()
            return value

    async def _import_or_create_strategy_config(self):
        current_strategy: str = in_memory_config_map.get("strategy").value
        strategy_file_path_cv: ConfigVar = in_memory_config_map.get("strategy_file_path")
        choice = await self.app.prompt(prompt="Import previous configs or create a new config file? "
                                              "(import/create) >>> ")
        if choice == "import":
            strategy_path = await self.app.prompt(strategy_file_path_cv.prompt)
            strategy_path = strategy_path
            self.app.log(f"Loading previously saved config file from {strategy_path}...")
        elif choice == "create":
            strategy_path = await copy_strategy_template(current_strategy)
            self.app.log(f"new config file at {strategy_path} created.")
        else:
            self.app.log('Invalid choice. Please enter "create" or "import".')
            strategy_path = await self._import_or_create_strategy_config()

        # Validate response
        if not strategy_file_path_cv.validate(strategy_path):
            self.app.log(f"Invalid path {strategy_path}. Please enter \"create\" or \"import\".")
            strategy_path = await self._import_or_create_strategy_config()
        return strategy_path

    async def _config_loop(self, keys: List[str] = []):
        self.app.log("Please follow the prompt to complete configurations: ")
        self.placeholder_mode = True
        self.app.toggle_hide_input()

        async def single_prompt(cvar: ConfigVar):
            if cvar.required:
                val = await self.app.prompt(prompt=cvar.prompt, is_password=cvar.is_secure)
                if not cvar.validate(val):
                    self.app.log("%s is not a valid %s value" % (val, cvar.key))
                    val = await single_prompt(cvar)
            else:
                val = cvar.value
            if val is None or len(val) == 0:
                val = cvar.default
            return val

        async def inner_loop(_keys: List[str]):
            for key in _keys:
                current_strategy: str = in_memory_config_map.get("strategy").value
                strategy_cm: Dict[str, ConfigVar] = get_strategy_config_map(current_strategy)
                if key in in_memory_config_map:
                    cv: ConfigVar = in_memory_config_map.get(key)
                elif key in global_config_map:
                    cv: ConfigVar = global_config_map.get(key)
                else:
                    cv: ConfigVar = strategy_cm.get(key)
                if key == "wallet":
                    wallets = list_wallets()
                    if len(wallets) > 0:
                        value = await self._unlock_wallet()
                    else:
                        value = await self._create_or_import_wallet()
                    logging.getLogger("hummingbot.public_eth_address").info(value)
                elif key == "strategy_file_path":
                    value = await self._import_or_create_strategy_config()
                else:
                    value = await single_prompt(cv)
                cv.value = parse_cvar_value(cv, value)
            if not self.config_complete:
                await inner_loop(self._get_empty_configs())
        try:
            await inner_loop(keys)
            await write_config_to_yml()
            self.app.log("\nConfig process complete. Enter \"start\" to start market making.")
        except asyncio.TimeoutError:
            self.logger().error("Prompt timeout")
        except Exception as err:
            self.logger().error("Unknown error while writing config. %s" % (err,), exc_info=True)
        finally:
            self.app.toggle_hide_input()
            self.placeholder_mode = False
            self.app.change_prompt(prompt=">>> ")

    def _initialize_wallet(self, token_symbols: List[str]):
        ethereum_rpc_url = global_config_map.get("ethereum_rpc_url").value
        erc20_token_addresses = get_erc20_token_addresses(token_symbols)
        self.wallet: Web3Wallet = Web3Wallet(private_key=self.acct.privateKey,
                                             backend_urls=[ethereum_rpc_url],
                                             erc20_token_addresses=erc20_token_addresses,
                                             chain=EthereumChain.MAIN_NET)

    def _initialize_markets(self, market_names: List[Tuple[str, str]]):
        ethereum_rpc_url = global_config_map.get("ethereum_rpc_url").value
        binance_api_key = global_config_map.get("binance_api_key").value
        binance_api_secret = global_config_map.get("binance_api_secret").value

        for market_name, symbol in market_names:
            market = None
            if market_name == "ddex":
                market = DDEXMarket(wallet=self.wallet,
                                    web3_url=ethereum_rpc_url,
                                    order_book_tracker_data_source_type=OrderBookTrackerDataSourceType.EXCHANGE_API,
                                    symbols=[symbol])

            elif market_name == "binance":
                market = BinanceMarket(web3_url=ethereum_rpc_url,
                                       binance_api_key=binance_api_key,
                                       binance_api_secret=binance_api_secret,
                                       order_book_tracker_data_source_type=OrderBookTrackerDataSourceType.EXCHANGE_API,
                                       symbols=[symbol])

            elif market_name == "radar_relay":
                market = RadarRelayMarket(wallet=self.wallet,
                                          web3_url=ethereum_rpc_url,
                                          symbols=[symbol])

            self.markets[market_name]: MarketBase = market

    def status(self) -> bool:
        if self.config_complete:
            self.app.log(" - Config complete")
        else:
            self.app.log(' x Pending config. Please enter "config" before starting the bot.')
            return False

        eth_node_valid = check_web3(global_config_map.get("ethereum_rpc_url").value)
        if eth_node_valid:
            self.app.log(" - Ethereum node running and current")
        else:
            self.app.log(' x Bad ethereum rpc url. Your node may be syncing. '
                         'Please re-configure by entering "config ethereum_rpc_url"')
            return False

        loading_markets: List[str] = []
        for market_name, market in self.markets.items():
            if not market.ready:
                loading_markets.append(market_name)
        if self.strategy is None:
            return True
        elif len(loading_markets) > 0:
            for loading_market in loading_markets:
                self.app.log(f" x Waiting for {loading_market} market to get ready for trading. "
                             f"Please keep the bot running and try to start again in a few minutes")
            return False
        else:
            self.app.log(" - All markets ready")

        if self.strategy is not None:
            self.app.log("\n" + self.strategy.format_status())

        return False

    def help(self, command):
        if command == 'all':
            self.app.log(self.parser.format_help())
        else:
            subparsers_actions = [
                action for action in self.parser._actions if isinstance(action, argparse._SubParsersAction)]

            for subparsers_action in subparsers_actions:
                subparser = subparsers_action.choices.get(command)
                self.app.log(subparser.format_help())

    def get_balance(self, currency: str = "WETH", wallet: bool = False, exchange: str = None):
        if wallet:
            if self.wallet is None:
                self.app.log('Wallet not available. Please configure your wallet (Enter "config wallet")')
            elif currency is None:
                self.app.log(f"{self.get_wallet_balance()}")
            else:
                self.app.log(self.wallet.get_balance(currency.upper()))
        elif exchange:
            if exchange in self.markets:
                if currency is None:
                    self.app.log(f"{self.get_exchange_balance(exchange)}")
                else:
                    self.app.log(self.markets[exchange].get_balance(currency.upper()))
            else:
                self.app.log('The exchange you entered has not been initialized. '
                             'You may check your exchange balance after entering the "start" command.')
        else:
            self.help("get_balance")

    def list(self, obj: str):
        if obj == "wallets":
            wallets = list_wallets()
            if len(wallets) == 0:
                self.app.log('Wallet not available. Please configure your wallet (Enter "config wallet")')
            else:
                self.app.log('\n'.join(wallets))
        elif obj == "exchanges":
            if len(EXCHANGES) == 0:
                self.app.log("No exchanges available")
            else:
                self.app.log('\n'.join(EXCHANGES))
        elif obj == "configs":
            self.app.log('\n'.join(load_required_configs().keys()))
        else:
            self.help("list")

    def describe(self, wallet: bool = False, exchange: str = None):
        if wallet:
            if self.wallet is None:
                self.app.log('None available. Your wallet may not have been initialized. Enter "start" to initialize '
                             'your wallet.')
            else:
                self.app.log(self.wallet.address)
                self.app.log(f"{self.get_wallet_balance()}")
        elif exchange is not None:
            if exchange in self.markets:
                self.app.log(f"{self.get_exchange_balance(exchange)}")
            else:
                raise InvalidCommandError("The exchange you specified has not been initialized")
        else:
            self.help("describe")

    def start(self, log_level: Optional[str] = None):
        is_valid = self.status()
        if not is_valid:
            return

        if log_level is not None:
            init_logging("hummingbot_logs.yml", override_log_level=log_level.upper())

        ExchangeRateConversion.get_instance().start()
        strategy_name = in_memory_config_map.get("strategy").value
        self.init_reporting_module()
        self.app.log(f"Status check complete. Starting '{strategy_name}' strategy...")
        asyncio.ensure_future(self.start_market_making(strategy_name))

    async def start_market_making(self, strategy_name: str):
        strategy_cm = get_strategy_config_map(strategy_name)
        if strategy_name == "cross_exchange_market_making":
            maker_market = strategy_cm.get("maker_market").value.lower()
            taker_market = strategy_cm.get("taker_market").value.lower()
            raw_maker_symbol = strategy_cm.get("maker_market_symbol").value.upper()
            raw_taker_symbol = strategy_cm.get("taker_market_symbol").value.upper()
            min_profitability = strategy_cm.get("min_profitability").value
            trade_size_override = strategy_cm.get("trade_size_override").value
            strategy_report_interval = global_config_map.get("strategy_report_interval").value
            limit_order_min_expiration = strategy_cm.get("limit_order_min_expiration").value
            cancel_order_threshold = strategy_cm.get("cancel_order_threshold").value
            active_order_canceling = strategy_cm.get("active_order_canceling").value
            top_depth_tolerance_rules = [(re.compile(re_str), value)
                                         for re_str, value
                                         in strategy_cm.get("top_depth_tolerance").value]
            top_depth_tolerance = 0.0

            for regex, tolerance_value in top_depth_tolerance_rules:
                if regex.match(raw_maker_symbol) is not None:
                    top_depth_tolerance = tolerance_value

            try:
                maker_assets: Tuple[str, str] = SymbolSplitter.split(maker_market, raw_maker_symbol)
                taker_assets: Tuple[str, str] = SymbolSplitter.split(taker_market, raw_taker_symbol)
            except ValueError as e:
                self.app.log(str(e))
                return

            market_names: List[Tuple[str, str]] = [
                (maker_market, raw_maker_symbol),
                (taker_market, raw_taker_symbol)
            ]

            self._initialize_wallet(token_symbols=list(set(maker_assets + taker_assets)))
            self._initialize_markets(market_names)

            self.market_pair = CrossExchangeMarketPair(*([self.markets[maker_market], raw_maker_symbol] + list(maker_assets) +
                                                  [self.markets[taker_market], raw_taker_symbol] + list(taker_assets) +
                                                  [top_depth_tolerance]))

            strategy_logging_options = (CrossExchangeMarketMakingStrategy.OPTION_LOG_CREATE_ORDER |
                                        CrossExchangeMarketMakingStrategy.OPTION_LOG_ADJUST_ORDER |
                                        CrossExchangeMarketMakingStrategy.OPTION_LOG_MAKER_ORDER_FILLED |
                                        CrossExchangeMarketMakingStrategy.OPTION_LOG_REMOVING_ORDER |
                                        CrossExchangeMarketMakingStrategy.OPTION_LOG_STATUS_REPORT |
                                        CrossExchangeMarketMakingStrategy.OPTION_LOG_MAKER_ORDER_HEDGED)

            self.strategy = CrossExchangeMarketMakingStrategy(market_pairs=[self.market_pair],
                                                              min_profitability=min_profitability,
                                                              status_report_interval=strategy_report_interval,
                                                              logging_options=strategy_logging_options,
                                                              trade_size_override=trade_size_override,
                                                              limit_order_min_expiration=limit_order_min_expiration,
                                                              cancel_order_threshold=cancel_order_threshold,
                                                              active_order_canceling=active_order_canceling
                                                              )

        elif strategy_name == "arbitrage":
            primary_market = strategy_cm.get("primary_market").value.lower()
            secondary_market = strategy_cm.get("secondary_market").value.lower()
            raw_primary_symbol = strategy_cm.get("primary_market_symbol").value.upper()
            raw_secondary_symbol = strategy_cm.get("secondary_market_symbol").value.upper()
            min_profitability = strategy_cm.get("min_profitability").value

            try:
                primary_assets: Tuple[str, str] = SymbolSplitter.split(primary_market, raw_primary_symbol)
                secondary_assets: Tuple[str, str] = SymbolSplitter.split(secondary_market, raw_secondary_symbol)

            except ValueError as e:
                self.app.log(str(e))
                return
            market_names: List[Tuple[str, str]] = [(primary_market, raw_primary_symbol),
                                                   (secondary_market, raw_secondary_symbol)]
            self._initialize_wallet(token_symbols=list(set(primary_assets + secondary_assets)))
            self._initialize_markets(market_names)
            self.market_pair = ArbitrageMarketPair(*([self.markets[primary_market], raw_primary_symbol] +
                                                     list(primary_assets) +
                                                     [self.markets[secondary_market], raw_secondary_symbol] +
                                                     list(secondary_assets)))

            strategy_logging_options = ArbitrageStrategy.OPTION_LOG_ALL

            self.strategy = ArbitrageStrategy(market_pairs=[self.market_pair],
                                              min_profitability=min_profitability,
                                              logging_options=strategy_logging_options)

        else:
            raise NotImplementedError

        try:
            self.clock = Clock(ClockMode.REALTIME)
            self.clock.add_iterator(self.wallet)
            for market in self.markets.values():
                self.clock.add_iterator(market)
            self.clock.add_iterator(self.strategy)
            self.strategy_task: asyncio.Task = asyncio.ensure_future(self.clock.run())
            self.app.log(f"\n'{strategy_name}' strategy started.\n"
                         f"You can use the `status` command to query the progress.")
        except Exception as e:
            self.logger().error(str(e), exc_info=True)

    async def stop(self, skip_order_cancellation: bool = False):
        self.app.log("\nWinding down...")
        if not skip_order_cancellation:
            success = await self._cancel_outstanding_orders()
            if success:
                # Only erase markets when cancellation has been successful
                self.markets = {}
        if self.reporting_module:
            self.reporting_module.stop()
        if self.strategy_task is not None and not self.strategy_task.cancelled():
            self.strategy_task.cancel()
        self.wallet = None
        self.strategy_task = None
        self.strategy = None
        self.market_pair = None
        self.clock = None

    async def exit(self, force: bool = False):
        if self.strategy_task is not None and not self.strategy_task.cancelled():
            self.strategy_task.cancel()
        if force is False:
            success = await self._cancel_outstanding_orders()
            if not success:
                self.app.log('Wind down process terminated: Failed to cancel all outstanding orders. '
                             '\nYou may need to manually cancel remaining orders by logging into your chosen exchanges'
                             '\n\nTo force exit the app, enter "exit -f"')
                return
            # Freeze screen 1 second for better UI
            await asyncio.sleep(1)
        self.app.exit()



