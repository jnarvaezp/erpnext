# ERPNext - web based ERP (http://erpnext.com)
# Copyright (C) 2012 Web Notes Technologies Pvt Ltd
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals
import webnotes
from webnotes import _, msgprint
from webnotes.utils import flt, cint
import json

from buying.utils import get_item_details
from setup.utils import get_company_currency
from webnotes.model.utils import round_floats_in_doc

from controllers.accounts_controller import AccountsController

class BuyingController(AccountsController):
	def validate(self):		
		if self.meta.get_field("currency"):
			self.company_currency = get_company_currency(self.doc.company)
			self.validate_conversion_rate("currency", "conversion_rate")
			
			if self.doc.price_list_name and self.doc.price_list_currency:
				self.validate_conversion_rate("price_list_currency", "plc_conversion_rate")
			
			# IMPORTANT: enable this only when client side code is similar to this one
			# self.calculate_taxes_and_totals()
			
			# set total in words
			self.set_total_in_words()
		
	def update_item_details(self):
		for item in self.doclist.get({"parentfield": self.fname}):
			ret = get_item_details({
				"doctype": self.doc.doctype,
				"docname": self.doc.name,
				"item_code": item.item_code,
				"warehouse": item.warehouse,
				"supplier": self.doc.supplier,
				"transaction_date": self.doc.posting_date,
				"conversion_rate": self.doc.conversion_rate,
				"price_list_name": self.doc.price_list_name,
				"price_list_currency": self.doc.price_list_currency,
				"plc_conversion_rate": self.doc.plc_conversion_rate
			})
			for r in ret:
				if not item.fields.get(r):
					item.fields[r] = ret[r]
	
	def validate_conversion_rate(self, currency_field, conversion_rate_field):
		"""common validation for currency and price list currency"""
		
		currency = self.doc.fields.get(currency_field)
		conversion_rate = flt(self.doc.fields.get(conversion_rate_field))
		conversion_rate_label = self.meta.get_label(conversion_rate_field)
		
		if conversion_rate == 0:
			msgprint(conversion_rate_label + _(' cannot be 0'), raise_exception=True)
		
		# parenthesis for 'OR' are necessary as we want it to evaluate as 
		# mandatory valid condition and (1st optional valid condition 
		# 	or 2nd optional valid condition)
		valid_conversion_rate = (conversion_rate and 
			((currency == self.company_currency and conversion_rate == 1.00)
				or (currency != self.company_currency and conversion_rate != 1.00)))

		if not valid_conversion_rate:
			msgprint(_('Please enter valid ') + conversion_rate_label + (': ') 
				+ ("1 %s = [?] %s" % (currency, self.company_currency)),
				raise_exception=True)

	def set_total_in_words(self):
		from webnotes.utils import money_in_words
		company_currency = get_company_currency(self.doc.company)
		if self.meta.get_field("in_words"):
			self.doc.in_words = money_in_words(self.doc.grand_total, company_currency)
		if self.meta.get_field("in_words_import"):
			self.doc.in_words_import = money_in_words(self.doc.grand_total_import,
		 		self.doc.currency)
		
	def calculate_taxes_and_totals(self):
		self.doc.conversion_rate = flt(self.doc.conversion_rate)
		self.item_doclist = self.doclist.get({"parentfield": self.fname})
		self.tax_doclist = self.doclist.get({"parentfield": "purchase_tax_details"})
		
		self.calculate_item_values()
		self.initialize_taxes()
		self.calculate_net_total()
		self.calculate_taxes()
		self.calculate_totals()
		self.calculate_outstanding_amount()
		
		self._cleanup()
		
	def calculate_item_values(self):
		def _set_base(item, print_field, base_field):
			"""set values in base currency"""
			item.fields[base_field] = flt((flt(item.fields[print_field],
				self.precision.item[print_field]) * self.doc.conversion_rate),
				self.precision.item[base_field])

		for item in self.item_doclist:
			round_floats_in_doc(item, self.precision.item)

			if item.discount == 100:
				if not item.import_ref_rate:
					item.import_ref_rate = item.import_rate
				item.import_rate = 0
			else:
				if item.import_ref_rate:
					item.import_rate = flt(item.import_ref_rate *
						(1.0 - (item.discount_rate / 100.0)),
						self.precision.item.import_rate)
				else:
					# assume that print rate and discount are specified
					item.import_ref_rate = flt(item.import_rate / 
						(1.0 - (item.discount_rate / 100.0)),
						self.precision.item.import_ref_rate)

			item.import_amount = flt(item.import_rate * item.qty,
				self.precision.item.import_amount)

			_set_base(item, "import_ref_rate", "purchase_ref_rate")
			_set_base(item, "import_rate", "rate")
			_set_base(item, "import_amount", "amount")
		
	def initialize_taxes(self):
		for tax in self.tax_doclist:
			# initialize totals to 0
			tax.tax_amount = tax.total = 0.0
			
			# temporary fields
			tax.tax_amount_for_current_item = tax.grand_total_for_current_item = 0.0
			
			tax.item_wise_tax_detail = {}
			
			self.validate_on_previous_row(tax)
			
			round_floats_in_doc(tax, self.precision.tax)
		
	def calculate_net_total(self):
		self.doc.net_total = 0
		self.doc.net_total_import = 0

		for item in self.item_doclist:
			self.doc.net_total += item.amount
			self.doc.net_total_import += item.import_amount
			
		self.doc.net_total = flt(self.doc.net_total, self.precision.main.net_total)
		self.doc.net_total_import = flt(self.doc.net_total_import,
			self.precision.main.net_total_import)
		
	def calculate_taxes(self):
		for item in self.item_doclist:
			item_tax_map = self._load_item_tax_rate(item.item_tax_rate)
			item.item_tax_amount = 0

			for i, tax in enumerate(self.tax_doclist):
				# tax_amount represents the amount of tax for the current step
				current_tax_amount = self.get_current_tax_amount(item, tax, item_tax_map)

				self.set_item_tax_amount(item, tax, current_tax_amount)

				# case when net total is 0 but there is an actual type charge
				# in this case add the actual amount to tax.tax_amount
				# and tax.grand_total_for_current_item for the first such iteration
				if not (current_tax_amount or self.doc.net_total or tax.tax_amount) and \
						tax.charge_type=="Actual":
					zero_net_total_adjustment = flt(tax.rate, self.precision.tax.tax_amount)
					current_tax_amount += zero_net_total_adjustment

				# store tax_amount for current item as it will be used for
				# charge type = 'On Previous Row Amount'
				tax.tax_amount_for_current_item = current_tax_amount

				# accumulate tax amount into tax.tax_amount
				tax.tax_amount += tax.tax_amount_for_current_item

				if tax.category == "Valuation":
					# if just for valuation, do not add the tax amount in total
					# hence, setting it as 0 for further steps
					current_tax_amount = 0
				else:
					current_tax_amount *= tax.add_deduct_tax == "Deduct" and -1.0 or 1.0

				# Calculate tax.total viz. grand total till that step
				# note: grand_total_for_current_item contains the contribution of 
				# item's amount, previously applied tax and the current tax on that item
				if i==0:
					tax.grand_total_for_current_item = flt(item.amount +
						current_tax_amount, self.precision.tax.total)

				else:
					tax.grand_total_for_current_item = \
						flt(self.tax_doclist[i-1].grand_total_for_current_item +
							current_tax_amount, self.precision.tax.total)

				# in tax.total, accumulate grand total of each item
				tax.total += tax.grand_total_for_current_item

				# store tax_breakup for each item
				# DOUBT: should valuation type amount also be stored?
				tax.item_wise_tax_detail[item.item_code] = current_tax_amount
		
	def calculate_totals(self):
		if self.tax_doclist:
			self.doc.grand_total = flt(self.tax_doclist[-1].total,
				self.precision.main.grand_total)
			self.doc.grand_total_import = flt(
				self.doc.grand_total / self.doc.conversion_rate,
				self.precision.main.grand_total_import)
		else:
			self.doc.grand_total = flt(self.doc.net_total,
				self.precision.main.grand_total)
			self.doc.grand_total_print = flt(
				self.doc.grand_total / self.doc.conversion_rate,
				self.precision.main.grand_total_import)

		self.doc.total_tax = \
			flt(self.doc.grand_total - self.doc.net_total,
			self.precision.main.total_tax)

		if self.meta.get_field("rounded_total"):
			self.doc.rounded_total = round(self.doc.grand_total)
		
		if self.meta.get_field("rounded_total_import"):
			self.doc.rounded_total_import = round(self.doc.grand_total_import)
			
	def calculate_outstanding_amount(self):
		if self.doc.doctype == "Purchase Invoice" and self.doc.docstatus == 0:
			self.doc.total_advance = flt(self.doc.total_advance,
				self.precision.main.total_advance)
			self.doc.outstanding_amount = flt(self.doc.grand_total - self.doc.total_advance,
				self.precision.main.outstanding_amount)
			
	def _cleanup(self):
		for tax in self.tax_doclist:
			del tax.fields["grand_total_for_current_item"]
			del tax.fields["tax_amount_for_current_item"]
			tax.item_wise_tax_detail = json.dumps(tax.item_wise_tax_detail)
		
		# except in purchase invoice, rate field is purchase_rate
		if self.doc.doctype != "Purchase Invoice":
			for item in self.item_doclist:
				item.purchase_rate = item.rate
				del item.fields["rate"]
		
	def validate_on_previous_row(self, tax):
		"""
			validate if a valid row id is mentioned in case of
			On Previous Row Amount and On Previous Row Total
		"""
		if tax.charge_type in ["On Previous Row Amount", "On Previous Row Total"] and \
				(not tax.row_id or cint(tax.row_id) >= tax.idx):
			msgprint((_("Row") + " # %(idx)s [%(taxes_doctype)s]: " + \
				_("Please specify a valid") + " %(row_id_label)s") % {
					"idx": tax.idx,
					"taxes_doctype": tax.parenttype,
					"row_id_label": self.meta.get_label("row_id",
						parentfield="purchase_tax_details")
				}, raise_exception=True)
				
	def _load_item_tax_rate(self, item_tax_rate):
		if not item_tax_rate:
			return {}
		return json.loads(item_tax_rate)
		
	def get_current_tax_amount(self, item, tax, item_tax_map):
		tax_rate = self._get_tax_rate(tax, item_tax_map)

		if tax.charge_type == "Actual":
			# distribute the tax amount proportionally to each item row
			actual = flt(tax.rate, self.precision.tax.tax_amount)
			current_tax_amount = (self.doc.net_total
				and ((item.amount / self.doc.net_total) * actual)
				or 0)
		elif tax.charge_type == "On Net Total":
			current_tax_amount = (tax_rate / 100.0) * item.amount
		elif tax.charge_type == "On Previous Row Amount":
			current_tax_amount = (tax_rate / 100.0) * \
				self.tax_doclist[cint(tax.row_id) - 1].tax_amount_for_current_item
		elif tax.charge_type == "On Previous Row Total":
			current_tax_amount = (tax_rate / 100.0) * \
				self.tax_doclist[cint(tax.row_id) - 1].grand_total_for_current_item

		return flt(current_tax_amount, self.precision.tax.tax_amount)
		
	def _get_tax_rate(self, tax, item_tax_map):
		if item_tax_map.has_key(tax.account_head):
			return flt(item_tax_map.get(tax.account_head), self.precision.tax.rate)
		else:
			return tax.rate
			
	def set_item_tax_amount(self, item, tax, current_tax_amount):
		"""
			item_tax_amount is the total tax amount applied on that item
			stored for valuation 
			
			TODO: rename item_tax_amount to valuation_tax_amount
		"""
		if tax.category in ["Valuation", "Valuation and Total"] and \
				item.item_code in self.stock_items:
			item.item_tax_amount += flt(current_tax_amount,
				self.precision.item.item_tax_amount)
	
	@property
	def stock_items(self):
		if not hasattr(self, "_stock_items"):
			item_codes = list(set(item.item_code for item in self.item_doclist))
			self._stock_items = [r[0] for r in webnotes.conn.sql("""select name
				from `tabItem` where name in (%s) and is_stock_item='Yes'""" % \
				(", ".join((["%s"]*len(item_codes))),), item_codes)]

		return self._stock_items
		
	@property
	def precision(self):
		if not hasattr(self, "_precision"):
			self._precision = webnotes._dict()
			self._precision.main = self.meta.get_precision_map()
			self._precision.item = self.meta.get_precision_map(parentfield = self.fname)
			if self.meta.get_field("purchase_tax_details"):
				self._precision.tax = self.meta.get_precision_map(parentfield = \
					"purchase_tax_details")
		return self._precision
