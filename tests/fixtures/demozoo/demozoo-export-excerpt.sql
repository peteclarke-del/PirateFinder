--
-- Synthetic excerpt in the layout of the Demozoo PostgreSQL export, for PirateFinder tests.
--

COPY public.demoscene_membership (id, member_id, group_id, is_current, data_source) FROM stdin;
1	81	70	f	\N
2	80	70	t	\N
3	82	10	t	\N
4	80	90	t	\N
\.

COPY public.demoscene_nick (id, releaser_id, name, abbreviation, differentiator) FROM stdin;
1	10	Skid Row	SR	
2	20	Effect		
3	30	MvA		
4	40	Some Group		
5	50	D-Bug		
6	60	Automation		
7	70	The Medway Boys		
8	90	Windows Crew		
9	65	Automation		
10	100	The Next Generation		
\.

COPY public.demoscene_releaser (id, name, is_group, notes, location, country_code) FROM stdin;
10	Skid Row	t	Formed by [Metallica](https://demozoo.org/sceners/2859/) in **1990**.[^1]\r\n\r\n\r\n[^1]: A cracktro.		
20	Effect	t			
30	MvA	t			
40	Some Group	t			
50	D-Bug	t	Rose from the ashes of <a href="http://demozoo.org/groups/2157/">Automation</a>.		
60	Automation	t	The Atari ST menu crew.		
65	Automation	t	An Amiga demo group of the same name.		
70	The Medway Boys	t	Started by Wurzel on the C64.		
80	Wurzel	f	A person.		
81	Gino	f			
82	Zodiac	f			
90	Windows Crew	t	Only Windows.		
100	The Next Generation	t			
\.

COPY public.demoscene_releaserexternallink (id, link_class, parameter, releaser_id, source) FROM stdin;
1	WikipediaPage	https://en.wikipedia.org/wiki/Skid_Row_%28warez_group%29	10	\N
2	WikipediaPage	http://de.wikipedia.org/wiki/Effect	20	\N
3	PouetGroup	871	10	\N
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
9	300	302	2	\N
7	400	401	1	\N
8	500	501	1	\N
\.

COPY public.productions_production (id, title, notes, release_date_date, release_date_precision, supertype) FROM stdin;
100	Compact 130		1992-04-17	d	production
101	Paperboy II +2		\N		production
102	Project X Mini Trainer		\N		production
200	Prevail Pack #147	Released at [The Party](https://demozoo.org/parties/1/) in *1993*.<br>Second line &amp; more	1993-12-01	m	production
201	Merry X-Mas		\N		production
202	Some Unreleased Chiptunes		\N		production
203	It's Us Again		\N		production
300	Crazy\tPack 5	line one\nline two	1990-01-01	y	production
301	Back\\Slash Intro		\N		production
302	Back\\Slash Intro		\N		production
400	Windows Pack 1		\N		production
401	Some Windows Demo		\N		production
500	D-BUG CD 193 A		2006-03-08	d	production
501	The Mindbomb Demo		\N		production
600	Empty Pack 1		\N		production
700	Automation CD #155 V2 intro	The menu of **CD 155**.	1990-11-02	d	production
701	Prevail Pack #148 intro		\N		production
702	Automation CD #156 intro		\N		production
703	Compact Menu 001 Intro		1988	y	production
704	Windows Menu 1 Intro		\N		production
705	Automation Megademo		\N		production
800	Lost Pack 7		\N		production
706	Menu #46 Intro		1991	y	production
707	Disk 3 Intro		\N		production
\.

COPY public.productions_production_author_nicks (id, production_id, nick_id) FROM stdin;
1	100	1
2	200	2
3	200	3
4	300	4
5	500	5
6	700	6
7	701	2
8	702	4
9	703	7
10	704	8
11	400	8
12	705	9
13	706	10
14	707	4
\.

COPY public.productions_production_platforms (id, production_id, platform_id) FROM stdin;
1	100	5
2	200	5
3	200	6
4	300	6
5	400	1
6	500	9
7	600	5
8	700	9
9	701	5
10	702	9
11	703	9
12	704	1
13	101	5
14	201	5
15	301	6
16	302	6
17	705	5
18	705	6
19	800	5
20	706	9
21	707	9
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
15	700	13
16	701	4
17	702	4
18	703	13
19	704	4
20	302	13
21	705	1
90	800	9
91	706	4
92	707	4
\.

COPY public.productions_productiontype (id, name, path, depth, numchild, "position", internal_name) FROM stdin;
1	Demo	0006	1	0	1	
4	Intro	000A	1	1	2	
13	Cracktro	000A0004	2	0	1	
14	Music	000D	1	1	3	music
29	Tracked Music	000D0006	2	0	1	tracked-music
9	Pack	000F	1	0	4	pack
\.

COPY public.productions_screenshot (id, production_id, original_url, original_width, original_height, thumbnail_url, thumbnail_width, thumbnail_height, standard_url, standard_width, standard_height, source_download_id, data_source, janeway_id, janeway_suffix) FROM stdin;
1	100	https://media.example/o/100.png	640	512	https://media.example/t/100.png	200	160	https://media.example/s/100.png	400	320	\N	\N	\N	\N
2	101	https://media.example/o/101.png	320	256	https://media.example/t/101.png	200	160	https://media.example/s/101.png	320	256	\N	\N	\N	\N
3	200	https://media.example/o/200a.png	320	256			\N		\N	\N	\N	\N	\N	\N
4	200	https://media.example/o/200b.png	640	512	https://media.example/t/200b.png	200	160	https://media.example/s/200b.png	400	320	\N	\N	\N	\N
5	400	https://media.example/o/400.png	640	480	https://media.example/t/400.png	200	150	https://media.example/s/400.png	400	300	\N	\N	\N	\N
6	700	https://media.example/o/700.png	320	200	https://media.example/t/700.png	200	125	https://media.example/s/700.png	320	200	\N	\N	\N	\N
7	201	https://media.example/o/201.png	320	256	https://media.example/t/201.png	200	160	https://media.example/s/201.png	320	256	\N	\N	\N	\N
9	200	https://media.example/o/200c.png	320	256			\N	https://media.example/s/200c.png	320	256	\N	\N	\N	\N
10	200	https://media.example/o/200d.png	320	256			\N	https://media.example/s/200d.png	320	256	\N	\N	\N	\N
11	301	https://media.example/o/301.png	320	256			\N		\N	\N	\N	\N	\N	\N
12	302	https://media.example/o/302.png	320	256			\N		\N	\N	\N	\N	\N	\N
8	201	https://media.example/o/201b.png	320	256	https://media.example/t/201b.png	200	160	https://media.example/s/201b.png	320	256	\N	\N	\N	\N
\.


COPY public.productions_productionlink (id, link_class, parameter, production_id, is_download_link, description) FROM stdin;
1	AmigascneFile	/Packdisks/Prevail/PrevailPack147.dms	200	t	
2	UntergrundFile	/users/someone/prevail147.zip	200	t	
3	AmigascneFile	/Groups/P/Prevail/Prevail-Intro	200	t	
4	SceneOrgFile	/demos/groups/prevail/prevail147.lha	200	t	
5	AmigascneFile	/Packdisks/Prevail/NotADownload.adf	200	f	
6	AmigascneFile	/Packdisks/Lost/LostPack07.adf	800	t	
7	FujiologyFile	/ST/D/DBUG/DBUG193A.ZIP	500	t	
8	FujiologyFile	/ST/A/AUTOMATN/AUTO155.ZIP	700	t	
9	SceneOrgFile	/demos/groups/dbug/dbug193a.zip	500	t	
\.
