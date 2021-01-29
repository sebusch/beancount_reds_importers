"""Generic investment importer module for beancount. Needs a reader module (eg: ofx, csv, etc.) from
beancount_reads_importers to work."""

import datetime
import itertools
import ntpath
import sys
import traceback
from functools import partial
from beancount.core import data
from beancount.core import amount
from beancount.ingest import importer
from beancount.core.position import CostSpec
from beancount_reds_importers.libimport import common

class Importer(importer.ImporterProtocol):
    def __init__(self, config):
        self.config = config
        self.initialized = False
        self.initialized_reader = False
        self.reader_ready = False
        self.custom_init_run = False
        self.includes_balances = False
        # REQUIRED_CONFIG = {
        #     'account_number'   : 'account number',
        #     'main_account'     : 'Destination account of import',
        #     'transfer'         : 'Account to which contributions and outgoing is transferred',
        #     'dividends'        : 'Account to book dividends',
        #     'cg'               : 'Account to book capital gains/losses',
        #     'fees'             : 'Account to book fees to',
        #     'rounding_error'   : 'Account to book rounding errors to',
        #     'fund_info '       : 'dictionary of fund info (by_id, money_market)'
        # }

    def initialize(self, file):
        if not self.initialized:
            self.custom_init()
            self.initialize_reader(file)
            if self.reader_ready:
                self.money_market_funds = self.config['fund_info']['money_market']
                self.fund_data = self.config['fund_info']['fund_data'] # [(ticker, id, long_name), ...]
                self.funds_by_id = {i: (ticker, desc) for ticker, i, desc in self.fund_data}
                self.funds_by_ticker = {ticker: (ticker, desc) for ticker, _, desc in self.fund_data}
                self.funds_db = getattr(self, self.funds_db_txt, 'funds_by_id')
                self.build_account_map() #TODO: avoid for identify()
            self.initialized = True

    def build_account_map(self):
        # transaction types: {'buymf', 'sellmf', 'buystock', 'sellstock', 'other', 'reinvest', 'income'}
        self.target_account_map = {
            "buymf":     self.config['main_account'],
            "sellmf":    self.config['main_account'],
            "buystock":  self.config['main_account'],
            "sellstock": self.config['main_account'],
            "reinvest":  self.config['dividends'],
            "dividends": self.config['dividends'],
            "income":    self.config['income'],
            "other":     self.config['transfer'],
            "credit":    self.config['transfer'],
            "debit":     self.config['transfer'],
            "transfer":  self.config['transfer'],
            "dep":       self.config['transfer'],
        }

    def custom_init(self):
        if not self.custom_init_run:
            self.max_rounding_error = 0.04
            self.account_number_field = 'number'
            self.filename_identifier_substring = 'bank_specific_filename.qfx'
            self.custom_init_run = True

    def get_ticker_info(self, security_id):
        return security_id, 'UNKNOWN'

    def get_ticker_info_from_id(self, security_id):
        try:
            ticker, ticker_long_name = self.funds_db[security_id]
        except KeyError:
            securities = self.get_security_list()
            securities_missing = [s for s in securities if s not in self.funds_db]
            print(f"Error: fund info not found for: {securities_missing}", file=sys.stderr)
            import pdb; pdb.set_trace()
            sys.exit(1)
        return ticker, ticker_long_name

    def get_target_acct(self, transaction):
        return self.target_account_map.get(transaction.type, None)

    def get_security_list(self):
        tickers = set()
        for ot in self.get_transactions():
            if ot.type in ['buymf', 'sellmf', 'buystock', 'sellstock', 'reinvest', 'income']:
                tickers.add(ot.security)
        return tickers

    # --------------------------------------------------------------------------------
    def generate_trade_entry(self, ot, file, counter):
        """ One of: ['buymf', 'sellmf', 'buystock', 'sellstock', 'reinvest', 'income']"""
        config = self.config
        # Build metadata
        ticker, ticker_long_name = self.get_ticker_info(ot.security)
        is_money_market = ticker in self.money_market_funds
        metadata = data.new_metadata(file.name, next(counter))
        if getattr(ot, 'settleDate', None) is not None:
            metadata['settlement_date'] = str(ot.settleDate.date())
        # Optional metadata, useful for debugging
        # metadata['type'] = ot.type

        if 'sell' in ot.type and not is_money_market:
            metadata['todo'] = 'TODO: this entry is incomplete until lots are selected (bean-doctor context <filename> <lineno>)'
        units = ot.units
        total = ot.total
        if 'sell' in ot.type:
            units = -1 * abs(ot.units)
        if ot.type in ['reinvest', 'dividends']:
            total = -1 * abs(ot.total)
        description = '[' + ticker + '] ' + ticker_long_name
        target_acct = self.get_target_acct(ot)

        # Build transaction entry
        entry = data.Transaction(metadata, ot.tradeDate.date(), self.FLAG,
                                 ot.memo, description, data.EMPTY_SET, data.EMPTY_SET, [])

        # Build postings
        if ot.type == 'income':  # cash
            data.create_simple_posting(entry, config['main_account'], total, self.currency)
            data.create_simple_posting(entry, target_acct, -1 * total, self.currency)
        else:  # stock/fund
            if is_money_market:
                common.create_simple_posting_with_price(entry, config['main_account'],
                                                        units, ticker, ot.unit_price, self.currency)
            elif 'sell' in ot.type:
                common.create_simple_posting_with_cost_or_price(entry, config['main_account'],
                                                                units, ticker, price_number=ot.unit_price,
                                                                price_currency=self.currency,
                                                                costspec=CostSpec(None, None, None, None, None, None))
                data.create_simple_posting(
                    entry, self.config['cg'], None, None)
            else:  # buy stock/fund
                common.create_simple_posting_with_cost(entry, config['main_account'],
                        units, ticker, ot.unit_price, self.currency)

            # TODO: resolve/remove this ugly hack
            reverser = 1
            if units > 0 and total > 0: #hack for some brokerages which have incorrect number signs
                reverser = -1
            data.create_simple_posting(entry, target_acct, reverser * total, self.currency)

            # Rounding errors
            rounding_error = (reverser * total) +  (ot.unit_price * units)
            if 0.0005 <= abs(rounding_error) <= self.max_rounding_error:
                data.create_simple_posting(
                    entry, config['rounding_error'], -1 * rounding_error, 'USD')
            # if abs(rounding_error) > self.max_rounding_error:
            #     print("Transactions legs do not sum up! Difference: {}. Entry: {}, ot: {}".format(
            #         rounding_error, entry, ot))

        return entry

    def generate_transfer_entry(self, ot, file, counter):
        """ Cash or in-kind transfers. One of: ['other', 'credit', 'debit', 'transfer', 'dep', 'income', 'dividends']"""
        config = self.config
        # Build metadata
        metadata = data.new_metadata(file.name, next(counter))
        target_acct = self.get_target_acct(ot)

        if ot.type == 'transfer' and ot.security:  # in-kind transfer
            ticker, ticker_long_name = self.get_ticker_info(ot.security)
            description = '[' + ticker + '] ' + ticker_long_name
            date = ot.tradeDate.date()
            units = ot.units
        else:  # cash transfer
            description = ot.type
            date = ot.date.date()
            units = ot.amount
            ticker = self.currency

        # Build transaction entry
        entry = data.Transaction(metadata, date, self.FLAG,
                                 ot.memo, description, data.EMPTY_SET, data.EMPTY_SET, [])

        # Build postings
        data.create_simple_posting(entry, config['main_account'], units, ticker)
        data.create_simple_posting(entry, target_acct, -1*units, ticker)
        return entry

    def extract_transactions(self, file, counter):
        # Required transaction fields:
        # 'type': 'buymf',
        # 'tradeDate': datetime.datetime(2018, 6, 25, 19, 0),
        # 'date': datetime.datetime(2018, 6, 25, 19, 0),
        # 'memo': 'MONEY FUND PURCHASE',
        # 'security': 'XXYYYZZ',
        # 'units': Decimal('2345.67'),
        # 'unit_price': Decimal('1.0'),
        # 'total': Decimal('-2345.67')

        # Optional transaction fields:
        # 'settleDate': datetime.datetime(2018, 6, 25, 19, 0),
        # 'commission': Decimal('0'),
        # 'fees': Decimal('0'),

        new_entries = []
        self.read_file(file)
        for ot in self.get_transactions():
            if ot.type in ['buymf', 'sellmf', 'buystock', 'sellstock', 'reinvest']:
                entry = self.generate_trade_entry(ot, file, counter)
            elif ot.type in ['other', 'credit', 'debit', 'transfer', 'dep', 'income', 'dividends']:
                entry = self.generate_transfer_entry(ot, file, counter)
            else:
                print("ERROR: unknown entry type:", ot.type)
                raise Exception('Unknown entry type')
            self.add_fee_postings(entry, ot)
            new_entries.append(entry)
        return new_entries

    def extract_balances_and_prices(self, file, counter):
        new_entries = []
        try:
            # date = self.ofx_account.statement.end_date.date() # this is the date of ofx download
            # we find the last transaction's date. If we use the ofx download date (if our source is ofx), we
            # could end up with a gap in time between the last transaction's date and balance assertion.
            # Pending (but not yet downloaded) transactions in this gap will get downloaded the next time we
            # do a download in the future, and cause the balance assertions to be invalid.
            date = max(ot.tradeDate if hasattr(ot, 'tradeDate') else ot.date
                       for ot in self.get_transactions()).date()
        except Exception as err:
            print("ERROR: no end_date. SKIPPING input.")
            traceback.print_tb(err.__traceback__)
            return []
        # balance assertions are evaluated at the beginning of the date, so move it to the following day
        date += datetime.timedelta(days=1)

        settlement_fund_balance = 0
        for pos in self.get_balance_positions():
            ticker, ticker_long_name = self.get_ticker_info(pos.security)
            meta = data.new_metadata(file.name, next(counter))
            balance_entry = data.Balance(meta, date, self.config['main_account'],
                                         amount.Amount(pos.units, ticker),
                                         None, None)
            new_entries.append(balance_entry)
            if ticker in self.money_market_funds:
                settlement_fund_balance = pos.units

            # extract price info if available
            if hasattr(pos, 'unit_price') and hasattr(pos, 'date'):
                meta = data.new_metadata(file.name, next(counter))
                price_entry = data.Price(meta, pos.date.date(), ticker,
                                         amount.Amount(pos.unit_price, self.currency))
                new_entries.append(price_entry)

        # we want trade date balance, which is reflected as USD
        #
        # trade date balance: The net dollar amount in your account that has not swept to or from your
        # settlement fund.
        #
        # available cash combines settlement fund and trade date balance
        balance = self.get_available_cash() - settlement_fund_balance
        meta = data.new_metadata(file.name, next(counter))
        balance_entry = data.Balance(meta, date, self.config['main_account'],
                                     amount.Amount(balance, self.currency),
                                     None, None)
        new_entries.append(balance_entry)
        return new_entries

    def add_fee_postings(self, entry, ot):
        config = self.config
        if hasattr(ot, 'fees') or hasattr(ot, 'commission'):
            if getattr(ot, 'fees', 0) != 0:
                data.create_simple_posting(entry, config['fees'], ot.fees, self.currency)
            if getattr(ot, 'commission', 0) != 0:
                data.create_simple_posting(entry, config['fees'], ot.commission, self.currency)

    def extract(self, file):
        self.initialize(file)
        counter = itertools.count()
        new_entries = []

        new_entries += self.extract_transactions(file, counter)
        if self.includes_balances:
            new_entries += self.extract_balances_and_prices(file, counter)

        return(new_entries)
