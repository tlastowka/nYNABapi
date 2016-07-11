import logging
from functools import wraps

from pynYNAB.connection import nYnabConnection
from pynYNAB.db.db import Session, CatalogBudget
from pynYNAB.schema.budget import Payee, Transaction, Budget, SubCategory
from pynYNAB.schema.catalog import BudgetVersion, Catalog
from pynYNAB.utils import chunk

logger = logging.getLogger('pynYnab')


class BudgetNotFound(Exception):
    pass


class nYnabClient(object):
    def __init__(self, nynabconnection, budget_name):
        if budget_name is None:
            logger.error('No budget name was provided')
            exit(-1)
        self.budget_name = budget_name
        self.connection = nynabconnection

        self.catalog = Catalog()
        self.catalog.sync(self.connection)

        catalogbudget = Session.query(CatalogBudget).filter(CatalogBudget.budget_name == self.budget_name).first()
        if not catalogbudget:
            raise BudgetNotFound()
        self.budget_version = Session.query(BudgetVersion).filter(BudgetVersion.budget_id == catalogbudget.id).first()

        self.budget = Budget()
        self.budget.budget_version_id = self.budget_version.id

        Session.add(self.budget)
        Session.add(self.budget_version)
        self.sync()

    def sync(self):
        logger.info('Syncing with the server....')
        # ending-starting represents the number of modifications that have been done to the data ?
        self.catalog.sync(self.connection)

        if self.budget_version is None and self.budget_name is not None:
            raise BudgetNotFound()
        else:
            self.budget.sync(self.connection)

    def operation(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            fn(self, *args, **kwargs)
            self.sync()

        return wrapped

    @operation
    def add_account(self, account, balance, balance_date):
        payee = Payee(
            entities_account_id=account.id,
            enabled=True,
            auto_fill_subcategory_enabled=True,
            auto_fill_memo_enabled=False,
            auto_fill_amount_enabled=False,
            rename_on_import_enabled=False,
            name="Transfer : %s" % account.account_name
        )
        immediateincomeid = next(
            s.id for s in self.budget.be_subcategories if s.internal_name == 'Category/__ImmediateIncome__')
        startingbalanceid = next(p.id for p in self.budget.be_payees if p.internal_name == 'StartingBalancePayee')

        transaction = Transaction(
            accepted=True,
            amount=balance,
            entities_subcategory_id=immediateincomeid,
            cleared='Cleared',
            date=balance_date,
            entities_account_id=account.id,
            entities_payee_id=startingbalanceid,
            is_tombstone=False
        )

        self.budget.be_accounts.append(account)
        self.budget.be_payees.append(payee)
        self.budget.be_transactions.append(transaction)

    @operation
    def delete_account(self, account):
        self.budget.be_accounts.remove(account)

    @operation
    def add_transaction(self, transaction):
        self.budget.be_transactions.append(transaction)

    def add_transactions(self, transaction_list):
        for chunkelement in chunk(transaction_list, 50):
            self._add_transactions(chunkelement)

    @operation
    def _add_transactions(self, transaction_list):
        for transaction in transaction_list:
            self.budget.be_transactions.append(transaction)

    @operation
    def delete_transaction(self, transaction):
        self.budget.be_transactions.remove(transaction)

    @operation
    def delete_budget(self, budget_name):
        for budget in self.catalog.ce_budgets:
            if budget.budget_name == budget_name:
                self.catalog.ce_budgets.remove(budget)

    def select_budget(self, budget_name):
        self.catalog.sync(self.connection)
        for budget_version in self.catalog.ce_budget_versions:
            budget = Session.query(BudgetVersion).get(budget_version.budget_id)
            #budget = self.catalog.ce_budgets.get(budget_version.budget_id)
            if budget.budget_name == budget_name:
                self.budget.budget_version_id = budget_version.id
                self.sync()
                break

    def create_budget(self, budget_name):
        import json
        currency_format = dict(
            iso_code='USD',
            example_format='123,456.78',
            decimal_digits=2,
            decimal_separator='.',
            symbol_first=True,
            group_separator=',',
            currency_symbol='$',
            display_symbol=True
        )
        date_format = dict(
            format='MM/DD/YYYY'
        )
        self.connection.dorequest(opname='CreateNewBudget',
                                  request_dic={
                                      "budget_name": budget_name,
                                      "currency_format": json.dumps(currency_format),
                                      "date_format": json.dumps(date_format)
                                  })

    @operation
    def clean_transactions(self):
        for transaction in self.budget.be_transactions:
            self.budget.be_transactions.delete(transaction)
        for subtransaction in self.budget.be_subtransactions:
            self.budget.be_subtransactions.delete(subtransaction)

    @operation
    def clean_budget(self):
        self.clean_transactions()
        for sub_category in [sub_category for sub_category in self.budget.be_subcategories if
                             sub_category.internal_name is None]:
            self.budget.be_subcategories.delete(sub_category)
        for mastercategory in [mastercategory for mastercategory in self.budget.be_master_categories if
                               mastercategory.deletable]:
            self.budget.be_master_categories.delete(mastercategory)
        self.clean_transactions()
        for payee in [payee for payee in self.budget.be_payees if payee.internal_name is None]:
            self.budget.be_payees.delete(payee)
        for account in self.budget.be_accounts:
            self.budget.be_accounts.delete(account)
