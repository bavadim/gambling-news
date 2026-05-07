# Шаблоны CSV для парсинга данных

## polymarket_raw.csv
Парсится с Polymarket API (gamma-api.polymarket.com, clob-api.polymarket.com)
Содержит market_question, вероятности, объем, ликвидность, спред

## rbc_raw.csv  
Парсится с РБК RSS (rssexport.rbc.ru/rbcnews/news/30/full.rss)
Содержит headline, summary, метаданные новости

## match_queue.csv
Связывает rbc_item_id ↔ market_id
Ручной или полуавтоматический матчинг

## computed.csv (дополнительный)
Вычисляемые поля из historical prices:
- yes_delta24_pp: изменение YES за 24ч
- yes_delta7_pp: изменение YES за 7д