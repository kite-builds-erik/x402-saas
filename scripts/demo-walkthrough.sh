#!/bin/bash
# scripts/demo-walkthrough.sh — narrated walkthrough of the x402-saas
# proxy flow: HTTP 402 negotiation, signed payment, upstream forward,
# 99/1 settlement, event ledger.
#
# Scripted (deterministic timings + colors) so the asciinema cast is
# repeatable. To run the LIVE version against your dashboard tenant:
#   curl https://<your-slug>.kite.dev/forecast

set -e

BOLD=$'\033[1m'
DIM=$'\033[2m'
GREEN=$'\033[32m'
CYAN=$'\033[36m'
YELLOW=$'\033[33m'
MAGENTA=$'\033[35m'
RESET=$'\033[0m'

p() { printf '%b\n' "$1"; sleep "${2:-0.4}"; }

clear

p "${BOLD}x402-saas — hosted x402 paywalls on Base${RESET}"                    0.5
p "${DIM}Sign up with a wallet, get a paywalled proxy URL in 60 seconds.${RESET}" 0.6
p ""                                                                          0.3

p "${CYAN}#${RESET} ${DIM}1. agent (customer) calls a paid endpoint${RESET}"  0.4
p "${BOLD}\$ curl -i https://acme.kite.dev/forecast${RESET}"                  0.5
p ""                                                                          0.3
p "${YELLOW}HTTP/1.1 402 Payment Required${RESET}"                             0.3
p "${DIM}content-type: application/json${RESET}"                              0.3
p ""                                                                          0.2
p "  {"                                                                       0.1
p "    ${MAGENTA}\"x402Version\"${RESET}: 1,"                                  0.1
p "    ${MAGENTA}\"accepts\"${RESET}: [{"                                      0.1
p "      ${MAGENTA}\"scheme\"${RESET}: ${GREEN}\"exact\"${RESET},"             0.1
p "      ${MAGENTA}\"network\"${RESET}: ${GREEN}\"base\"${RESET},"             0.1
p "      ${MAGENTA}\"maxAmountRequired\"${RESET}: ${GREEN}\"50000\"${RESET},"  0.1
p "      ${MAGENTA}\"asset\"${RESET}: ${GREEN}\"0x833…\"${RESET},   ${DIM}# USDC on Base${RESET}"  0.1
p "      ${MAGENTA}\"payTo\"${RESET}: ${GREEN}\"0xC504…60Bdf\"${RESET}"        0.1
p "    }]"                                                                    0.1
p "  }"                                                                       0.5

p ""                                                                          0.2
p "${CYAN}#${RESET} ${DIM}2. agent signs EIP-3009 transfer-with-auth, retries${RESET}" 0.4
p "${BOLD}\$ curl -i https://acme.kite.dev/forecast \\\\${RESET}"             0.3
p "${BOLD}    -H 'X-PAYMENT: eyJ4NDAyVmVyc2lvbiI6MSwic2NoZW1lIjoiZXhhY3Qi…'${RESET}" 0.5
p ""                                                                          0.3
p "${GREEN}HTTP/1.1 200 OK${RESET}"                                            0.3
p "${DIM}content-type: application/json${RESET}"                              0.3
p "${DIM}x402-settle-tx: 0x7ad9${RESET}…${DIM}    # ← real Base mainnet tx${RESET}"   0.4
p ""                                                                          0.2
p "  {"                                                                       0.1
p "    ${MAGENTA}\"city\"${RESET}: ${GREEN}\"Stavanger\"${RESET},"             0.1
p "    ${MAGENTA}\"temp\"${RESET}: 12,"                                        0.1
p "    ${MAGENTA}\"forecast\"${RESET}: ${GREEN}\"showers\"${RESET}"            0.1
p "  }"                                                                       0.5

p ""                                                                          0.3
p "${CYAN}#${RESET} ${DIM}3. behind the scenes, the proxy settled the USDC${RESET}"   0.4
p "  ${GREEN}→ 49500${RESET} USDC base units routed to ${BOLD}0xC504…60Bdf${RESET} ${DIM}(tenant)${RESET}" 0.4
p "  ${GREEN}→ 500${RESET}   USDC base units routed to ${BOLD}x402-saas treasury${RESET} ${DIM}(1% take-rate)${RESET}" 0.4
p "  ${GREEN}→ event logged to the tenant's dashboard${RESET}"                0.7

p ""                                                                          0.3
p "${BOLD}${GREEN}✓ pay-per-call. one HTTP round-trip. no SaaS account, no API key.${RESET}" 0.6
p ""                                                                          0.3
p "${DIM}The agent never registered. The merchant configured one route. The proxy did the rest.${RESET}" 0.8
p ""                                                                          0.3
