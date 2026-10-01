"""Weekly finance markets: "will X hit $H this week" touch markets.

Kept apart from the daily trader: own tables (schema `weekly`), own recorder
process.  Token-level price history and books share public.pm_history /
public.pm_books (token ids are unique).
"""
