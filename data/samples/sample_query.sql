SELECT o.order_id, c.customer_name, o.order_date, o.total_amount
FROM orders o, customers c
WHERE TO_CHAR(o.order_date, 'YYYY-MM-DD') = '2026-09-01'
  AND o.customer_id = c.customer_id
  AND UPPER(c.customer_name) LIKE 'KIM%';
