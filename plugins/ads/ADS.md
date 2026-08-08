# Sponsored arenas

Brands sponsor arenas on topics they want adjacency to.
A sponsored arena keeps the game identical and adds a light brand skin, an accent color plus the sponsor name.
The share card a winner posts to X carries the sponsor mark, so every share is brand reach next to the topic the brand chose.
Sponsorship lives in a small static config in arenas.py, keyed by topic. Topics without a sponsor get the default arcade look.
The safety gates run before any arena is served, sponsored or not, so a brand never sits next to a round that failed screening.
The pricing idea is per impression of the arena and the share card. Any figure attached to that idea would be illustrative only.
The demo ships zero real numbers. The cpm_note field is the literal string ILLUSTRATIVE so no invented market figure appears anywhere.
Every sponsor name in the config is a fictional demo brand.
