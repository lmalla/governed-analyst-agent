-- Denormalizes region from the customer, same rationale as stg_orders.sql.
select
    t.id as ticket_id,
    t.customer_id,
    t.created_at,
    t.category,
    t.priority,
    t.body,
    c.region
from {{ ref('raw_support_tickets') }} t
left join {{ ref('stg_customers') }} c on t.customer_id = c.customer_id
