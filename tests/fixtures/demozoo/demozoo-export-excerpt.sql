--
-- Synthetic excerpt in the layout of the Demozoo PostgreSQL export, for PirateFinder tests.
--

COPY public.demoscene_nick (id, releaser_id, name, abbreviation, differentiator) FROM stdin;
1	10	Skid Row	SR	
2	20	Effect		
3	30	MvA		
4	40	Some Group		
5	50	D-Bug		
\.

COPY public.platforms_platform (id, name, intro_text, photo) FROM stdin;
1	Windows	\N	
5	Amiga OCS/ECS	\N	
6	Amiga AGA	\N	
9	Atari ST/E	\N	
\.

COPY public.productions_packmember (id, pack_id, member_id, "position", data_source) FROM stdin;
1	100	102	2	\N
2	100	101	1	\N
3	200	201	1	\N
4	200	202	2	\N
5	200	203	3	\N
6	300	301	1	\N
7	400	401	1	\N
8	500	501	1	\N
\.

COPY public.productions_production (id, title, notes, release_date_date, release_date_precision, supertype) FROM stdin;
100	Compact 130		1992-04-17	d	production
101	Paperboy II +2		\N		production
102	Project X Mini Trainer		\N		production
200	Prevail Pack #147		1993-12-01	m	production
201	Merry X-Mas		\N		production
202	Some Unreleased Chiptunes		\N		production
203	It's Us Again		\N		production
300	Crazy\tPack 5	line one\nline two	1990-01-01	y	production
301	Back\\Slash Intro		\N		production
400	Windows Pack 1		\N		production
401	Some Windows Demo		\N		production
500	D-BUG CD 193 A		2006-03-08	d	production
501	The Mindbomb Demo		\N		production
600	Empty Pack 1		\N		production
\.

COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;
1	100	1
2	200	2
3	200	3
4	300	4
5	500	5
\.

COPY public.productions_production_platforms (id, production_id, platform_id) FROM stdin;
1	100	5
2	200	5
3	200	6
4	300	6
5	400	1
6	500	9
7	600	5
\.

COPY public.productions_production_types (id, production_id, productiontype_id) FROM stdin;
1	100	9
2	101	13
3	102	13
4	200	9
5	201	1
6	202	29
7	203	4
8	300	9
9	301	13
10	400	9
11	401	1
12	500	9
13	501	1
14	600	9
\.

COPY public.productions_productiontype (id, name, path, depth, numchild, "position", internal_name) FROM stdin;
1	Demo	0006	1	0	1	
4	Intro	000A	1	1	2	
13	Cracktro	000A0004	2	0	1	
14	Music	000D	1	1	3	music
29	Tracked Music	000D0006	2	0	1	tracked-music
9	Pack	000F	1	0	4	pack
\.

