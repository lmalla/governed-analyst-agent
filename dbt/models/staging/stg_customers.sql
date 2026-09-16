select
    id as customer_id,
    full_name,
    email,
    phone,
    region,
    signup_date,
    tier
from {{ ref('raw_customers') }}
