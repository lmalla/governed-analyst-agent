-- Denormalizes region from the customer, so the mart's row access policy
-- (Plan B) can use the same `region = 'East'` predicate as customers,
-- instead of a correlated subquery per table (see BUILD_SPEC.md §5).
select
    o.id as order_id,
    o.customer_id,
    o.order_date,
    o.amount,
    o.status,
    c.region
from {{ ref('raw_orders') }} o
left join {{ ref('stg_customers') }} c on o.customer_id = c.customer_id
