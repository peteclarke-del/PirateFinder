/*M!999999\- enable the sandbox mode */
-- Synthetic excerpt in the layout of the Atari Legend MariaDB dump.
-- Table and column names are the real ones; the rows are made up.

CREATE TABLE `news` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `text` mediumtext DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `news` VALUES
(1,'A semicolon; a quote \' and a doubled one \'\' and a bracket ('),
(2,'Spans
INSERT INTO `menus` VALUES (99,NULL,NULL,1,NULL,NULL,NULL,1);
two lines');
CREATE TABLE `menu_sets` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `name` varchar(64) NOT NULL,
  `menus_sort` enum('asc','desc') NOT NULL DEFAULT 'asc' COMMENT 'How to sort menus of this set',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_sets` VALUES
(13,'2021-04-11 13:44:50','2021-04-11 13:44:50','Automation (fake menus)','asc'),
(89,NULL,NULL,'Flame Of Finland/Superior','asc'),
(155,NULL,NULL,'Pompey Pirates','asc'),
(400,NULL,NULL,'Bob\'s Menus','asc');
CREATE TABLE `menus` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `number` int(11) DEFAULT NULL COMMENT 'Sequence number within the menu set',
  `issue` varchar(16) DEFAULT NULL COMMENT 'Menu issue (number or letter, etc.)',
  `date` date DEFAULT NULL COMMENT 'Release date',
  `version` varchar(8) DEFAULT NULL,
  `menu_set_id` bigint(20) unsigned NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menus` VALUES (3041,NULL,NULL,NULL,NULL,NULL,NULL,155),(3042,NULL,NULL,1,NULL,NULL,'1',155),(3043,NULL,NULL,1,NULL,NULL,'2',155),(3056,NULL,NULL,13,NULL,NULL,NULL,155);
INSERT INTO `menus` (`id`,`created_at`,`updated_at`,`number`,`issue`,`date`,`version`,`menu_set_id`) VALUES
(208,NULL,NULL,22,NULL,NULL,'bis',13),
(1000,NULL,NULL,54,NULL,'1991-01-26',NULL,89),
(1001,NULL,NULL,55,NULL,NULL,NULL,89),
(5000,NULL,NULL,NULL,'B',NULL,NULL,400);
CREATE TABLE `menu_disks` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `menu_id` bigint(20) unsigned NOT NULL,
  `part` varchar(16) DEFAULT NULL COMMENT 'Arbitrary part identifier (e.g. A, B, C, or Part I, Part II, ...)',
  `scrolltext` text DEFAULT NULL COMMENT 'Content of the scrolltext',
  `donated_by_individual_id` int(11) DEFAULT NULL COMMENT 'Who donated this menu/dump',
  `menu_disk_condition_id` bigint(20) unsigned DEFAULT NULL,
  `menu_disk_dump_id` bigint(20) unsigned DEFAULT NULL,
  `notes` varchar(512) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_disks` VALUES
(3218,NULL,NULL,3041,NULL,NULL,NULL,4,1745,NULL),
(3219,NULL,NULL,3042,NULL,'IT\'S \"GREAT\"\nIT''S A BACKSLASH \\ AND
A REAL LINE BREAK; (NOT THE END)',NULL,4,1746,NULL),
(3220,NULL,NULL,3043,NULL,NULL,NULL,1,NULL,'Needs\r\nfixing'),
(3233,NULL,NULL,3056,'_1',NULL,NULL,4,1756,NULL),
(3234,NULL,NULL,3056,'_2',NULL,NULL,4,NULL,NULL),
(3235,NULL,NULL,3056,'_3',NULL,NULL,4,NULL,NULL),
(3236,NULL,NULL,3056,'_4A',NULL,NULL,4,NULL,NULL),
(3237,NULL,NULL,3056,'_4B',NULL,NULL,4,NULL,NULL),
(223,NULL,NULL,208,NULL,NULL,NULL,2,NULL,NULL),
(1744,NULL,NULL,1000,NULL,NULL,1631,3,NULL,NULL),
(1745,NULL,NULL,1001,NULL,NULL,NULL,4,NULL,NULL),
(6000,NULL,NULL,5000,NULL,NULL,NULL,4,NULL,NULL);
CREATE TABLE `menu_disk_contents` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `menu_disk_id` bigint(20) unsigned NOT NULL,
  `order` tinyint(4) NOT NULL,
  `game_release_id` int(11) DEFAULT NULL,
  `game_id` int(11) DEFAULT NULL,
  `menu_software_id` bigint(20) unsigned DEFAULT NULL,
  `subtype` varchar(64) DEFAULT NULL,
  `version` varchar(64) DEFAULT NULL,
  `requirements` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_disk_contents` VALUES
(6,NULL,NULL,3219,6,NULL,NULL,6,NULL,'2.1',NULL),
(1,NULL,NULL,3219,1,10,NULL,NULL,NULL,NULL,NULL),
(2,NULL,NULL,3219,2,12,NULL,NULL,NULL,NULL,'1 MB'),
(3,NULL,NULL,3219,3,NULL,3,NULL,'doc',NULL,NULL),
(4,NULL,NULL,3219,4,NULL,NULL,5,NULL,NULL,NULL),
(5,NULL,NULL,3219,5,10,NULL,NULL,'cheat code',NULL,NULL),
(7,NULL,NULL,3219,7,NULL,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE `menu_disk_dumps` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `format` enum('STX','MSA','RAW','SCP','ST') DEFAULT NULL,
  `sha512` varchar(128) DEFAULT NULL,
  `size` int(11) DEFAULT NULL COMMENT 'File size in bytes',
  `user_id` int(11) DEFAULT NULL COMMENT 'User who uploaded the dump',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_disk_dumps` VALUES (1745,NULL,NULL,'MSA','84208632C869F4A9',734211,-1),(1746,NULL,NULL,'MSA','a0aa0e26c2839776',799870,4),(1756,NULL,NULL,'ST','e4d4835b13b8',839680,NULL);
CREATE TABLE `menu_disk_conditions` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `name` varchar(64) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_disk_conditions` VALUES (1,NULL,NULL,'Missing'),(2,NULL,NULL,'Intro only or partially damaged'),(3,NULL,NULL,'Slightly damaged'),(4,NULL,NULL,'Intact');
CREATE TABLE `menu_software` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `menu_software_content_type_id` bigint(20) unsigned NOT NULL,
  `name` varchar(255) NOT NULL,
  `demozoo_id` int(11) DEFAULT NULL COMMENT 'ID of the DemoZoo production',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_software` VALUES (5,NULL,NULL,3,'Ripper',NULL),(6,NULL,NULL,2,'Synth Dream',69534);
CREATE TABLE `menu_software_content_types` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `name` varchar(64) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `menu_software_content_types` VALUES (1,NULL,NULL,'Game'),(2,NULL,NULL,'Demo'),(3,NULL,NULL,'Utility'),(7,NULL,NULL,'E-zine / Documentation');
CREATE TABLE `games` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(255) DEFAULT NULL COMMENT 'Main name of a game',
  `slug` varchar(255) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `games` VALUES (1,'New Zealand Story, The','new-zealand-story'),(2,'Rick Dangerous','rick-dangerous'),(3,'Stack Up','stack-up');
CREATE TABLE `game_facts` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `game_id` int(11) NOT NULL,
  `fact` mediumtext NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `game_facts` VALUES (2,2,'See [url=https://example.org/rick]the story[/url] and [url=https://example.org/]https://example.org/[/url].\r\n\r\n\r\n[b]Cheat:[/b] type [i]POOKIE[/i] [img=14x16]https://example.org/wink.gif[/img]'),(1,2,'A re-release of [game=6500]Westphaser[/game].'),(3,3,'   ');
CREATE TABLE `screenshots` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `imgext` varchar(11) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `screenshots` VALUES (319,'png'),(320,'PNG'),(321,'jpg'),(322,'png'),(323,'zip'),(4970,'png');
CREATE TABLE `screenshot_game` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `game_id` int(11) DEFAULT NULL,
  `screenshot_id` int(11) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `screenshot_game` VALUES (1,2,323),(2,2,319),(3,2,320),(4,2,321),(5,2,322),(6,1,4970);
CREATE TABLE `menu_disk_screenshots` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `created_at` timestamp NULL DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT NULL,
  `menu_disk_id` bigint(20) unsigned NOT NULL,
  `imgext` varchar(4) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Screenshots of a menu disk';
INSERT INTO `menu_disk_screenshots` VALUES (1887,NULL,NULL,3219,'bmp'),(1886,NULL,NULL,3219,'png'),(1890,NULL,NULL,3220,'zip');
CREATE TABLE `game_akas` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `game_id` int(11) NOT NULL DEFAULT 0,
  `name` varchar(128) DEFAULT NULL,
  `language_id` char(2) DEFAULT NULL COMMENT 'Foreign key to language table',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `game_akas` VALUES (9,2,'Rick Dangerous 1',NULL);
CREATE TABLE `game_releases` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'Unique ID of a release',
  `game_id` int(11) NOT NULL COMMENT 'ID of the game the release is for',
  `name` varchar(255) DEFAULT NULL COMMENT 'Optional alternative name of the release',
  `date` date DEFAULT NULL COMMENT 'Release date',
  `license` enum('Commercial','Non-Commercial') DEFAULT NULL,
  `type` enum('Re-release','Budget','Budget re-release','Playable demo','Non-playable demo','Slideshow','Unofficial','Data disk','Review copy') DEFAULT NULL,
  `pub_dev_id` int(11) DEFAULT NULL COMMENT 'Publisher of the release',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `game_releases` VALUES (10,1,'',NULL,NULL,'Unofficial',NULL),(11,1,NULL,'1989-01-01','Commercial',NULL,411),(12,2,'Rick D',NULL,NULL,'Unofficial',NULL);
CREATE TABLE `game_release_akas` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'Unique ID of a game_release_aka',
  `game_release_id` int(11) NOT NULL COMMENT 'foreign key to game_release table',
  `name` varchar(256) DEFAULT NULL COMMENT 'Name of AKA',
  `language_id` char(2) DEFAULT NULL COMMENT 'foreign key to language table',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `game_release_akas` VALUES (1,12,'Rick Dangereux','fr');
CREATE TABLE `game_release_crew` (
  `game_release_id` int(11) NOT NULL COMMENT 'Foreign key to game_release table',
  `crew_id` int(11) NOT NULL COMMENT 'Foreign key to crew table'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `game_release_crew` VALUES (12,7);
CREATE TABLE `pub_devs` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `pub_devs` VALUES (411,'Ocean');
CREATE TABLE `crews` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `logo` varchar(255) DEFAULT NULL,
  `history` mediumtext DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `crews` VALUES (7,'Pompey Pirates','png','Founded in [b]Portsmouth[/b].\r\nIt\\\'s true.'),(8,'Superior',NULL,NULL),(9,'Flame of Finland',NULL,NULL),(10,'The Lonely Crew',NULL,'Never made a menu.');
CREATE TABLE `crew_individual` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `crew_id` int(11) DEFAULT NULL,
  `individual_id` int(11) DEFAULT NULL,
  `individual_nick_id` int(11) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `crew_individual` VALUES (2,7,1632,5),(1,7,1631,NULL),(3,7,1631,NULL);
CREATE TABLE `individual_nicks` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `individual_id` int(11) DEFAULT NULL,
  `nick_id` int(11) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `individual_nicks` VALUES (5,1632,1633);
CREATE TABLE `crew_menu_set` (
  `crew_id` int(11) NOT NULL,
  `menu_set_id` bigint(20) unsigned NOT NULL,
  PRIMARY KEY (`crew_id`,`menu_set_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Pivot between menu sets and crews';
INSERT INTO `crew_menu_set` VALUES (7,155),(8,89),(9,89);
CREATE TABLE `individuals` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `individuals` VALUES (1631,'Marcer'),(1632,'Alien'),(1633,'Big Al');
CREATE TABLE `trainer_options` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'Unique ID of trainer_options table',
  `name` varchar(256) DEFAULT NULL COMMENT 'Name of the option',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `trainer_options` VALUES (1,'Infinite Lives');
CREATE TABLE `game_release_trainer_option` (
  `id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'Unique ID of a game_release_trainer_options record',
  `game_release_id` int(11) NOT NULL COMMENT 'Foreign key to release table',
  `trainer_option_id` int(11) NOT NULL COMMENT 'Foreign key to trainer_options table',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT INTO `game_release_trainer_option` VALUES (1,12,1);
