#!/bin/sh
# Data for the README and site pictures: seed/example.sh plus some activity.
# Run on an empty database:  MAXPM_DB=/tmp/shots.db seed/screenshots.sh
set -e
D="$(dirname "$0")"
R="$D/../bin/maxpm -q"
"$D/example.sh" >/dev/null

$R register api-agent --note "backend work" >/dev/null
$R --as alex project describe website "Storefront pages in web/; React. Checkout, pricing, footer." >/dev/null
$R --as alex project describe backend "Orders service in api/; Python. Owns the orders API." >/dev/null
$R --as alex edit 5 --context "Footer links to /help and /terms return 404 since the docs moved to /docs/." \
  --touches web/components/Footer.tsx --check "npm test -- Footer" >/dev/null

$R --as alex add backend "Add order status webhooks" --doer ai --touches api/webhooks.py --after 2 >/dev/null  # 6
$R --as alex add website "Order history page" --doer ai --touches web/orders/ --after 2 >/dev/null              # 7
$R --as alex add website "Accessibility pass on checkout" -p 2 --doer ai --after 4 >/dev/null                   # 8

$R --as api-agent claim 1 >/dev/null
$R --as api-agent done 1 --output "api/orders.md: POST /orders, GET /orders/{id}, status enum" >/dev/null
$R --as api-agent claim 2 >/dev/null
$R --as alex send question "Is the orders API paginated? The order history page needs it." --item 2 >/dev/null

# Newer features: a question to the person, a pushed item, a timed blocker, a due date.
$R config set timezone UTC >/dev/null
$R register web-agent --note "storefront work" >/dev/null
$R --as api-agent send question --to alex "Do orders over \$500 need a manual review step?" --item 2 >/dev/null
$R --as alex push 5 --to web-agent --note "quick one before the checkout work" >/dev/null
$R --as alex add website "Publish the launch post" --doer human \
  --context "Post on the blog and the newsletter once checkout is live." >/dev/null                         # 9
$R --as alex blocked 9 --reason "Launch date is not set yet" --until 3d >/dev/null
$R --as alex edit 4 --due "$(date -u -v+5d +%Y-%m-%d 2>/dev/null || date -u -d +5days +%Y-%m-%d) UTC" >/dev/null
# The dashboard Board: an agent takes a person's item off their list.
$R --as alex add website "Resize the product photos" --doer human \
  --context "The photos in web/public/products/ are 4000 px wide." >/dev/null                            # 10
$R --as web-agent takeover 10 --note "A script in web/scripts/ can resize them" >/dev/null
# Goals: an outcome with a test for done, owned by one agent that plans and takes its items.
$R --as alex goal add website checkout-launch --outcome "Customers can pay for an order on the new checkout" \
  --done-when "A test order goes from cart to paid on staging" >/dev/null
for i in 4 7 8; do $R --as alex edit $i --goal checkout-launch >/dev/null; done
$R --as web-agent goal own checkout-launch >/dev/null
# A deploy target: finished work ships in one deploy item, after one review of the whole release.
$R target add storefront --description "web/ to Cloudflare Pages, api/ to Fly.io; a push to main deploys" >/dev/null
$R project target website storefront >/dev/null
$R project target backend storefront >/dev/null
$R --as api-agent target own storefront >/dev/null
$R config set review on >/dev/null
$R --as alex ship 1 >/dev/null
# Finished work for the Done tab, by an agent that holds nothing else.
$R register build-agent --note "build and CI work" >/dev/null
id() { $R --json --as alex add "$@" --doer ai | sed -n 's/^  "id": \([0-9]*\),*$/\1/p'; }
i=$(id website "Fix the cart total rounding")
$R --as build-agent claim "$i" >/dev/null
$R --as build-agent done "$i" --output "web/cart/total.ts: round once, at the end; 4 tests" >/dev/null
i=$(id backend "Set up the staging database")
$R --as build-agent claim "$i" >/dev/null
$R --as build-agent done "$i" --output "staging Postgres on Fly.io; migrations run on deploy" >/dev/null
echo "Loaded. Try: maxpm serve --open, maxpm status, maxpm go --project website"
