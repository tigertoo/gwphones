"""The tenant portal: a tenant's own staff managing their phone system.

A separate service from gwphones (the operator's side): its own users
database, and every phone action done through gwphones' API as a
restricted caller (X-Portal-Secret), the tenant fixed from the session.
See ../README.md.
"""
