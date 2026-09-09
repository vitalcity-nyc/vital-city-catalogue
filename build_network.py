#!/usr/bin/env python3
"""Build the unified, deconflicted PEOPLE dataset for the network explorer.

Fuses three sources, matching the same person across them by email (primary)
and name (fallback):
  - private/members_source.csv      Ghost members/subscribers (~10.9k)
  - private/contacts_source.xlsx    contact CRM, sheet "combined" (~1.25k, typed)
  - data/authors.json + catalogue   Vital City authors (article counts)

Writes (gitignored — sensitive):
  private/people.json         one record per person, deconflicted
  private/network_stats.json  headline counts + type x membership matrix

The plaintext never ships; encrypt_people.py produces the public encrypted blob.
"""
from datetime import datetime
import csv, json, re, unicodedata
from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
PERSON_CATS = ["VC contributor", "Senior contributor", "VC advisor", "journalist", "academic",
               "foundation leadership", "nonprofit leadership", "city gov",
               "current nyc.gov", "state gov", "fed gov", "judge"]
# Domain-area interests (specialties). "architecture" lives here (was a top-level type).
TOPIC_CATS = ["criminal justice", "housing", "transit", "budget", "urban planning",
              "education", "public health", "economy", "technology",
              "politics & government", "race & equity", "culture", "architecture"]
NONPERSON = {"vital city", "a survey", "a photo essay", "a conversation",
             "the editors", "editorial board", "vital city staff", "various"}

# Map an article's public topic tag (lowercased) -> a specialty domain. Used to
# infer a person's domain interests from what they've written for us.
TOPIC_MAP = {
    "crime": "criminal justice", "justice": "criminal justice",
    "police reform": "criminal justice", "policing": "criminal justice",
    "jails": "criminal justice", "incarceration": "criminal justice",
    "gun violence": "criminal justice", "subway crime": "criminal justice",
    "drugs": "criminal justice",
    "housing": "housing", "homelessness": "housing",
    "transit": "transit", "transportation": "transit",
    "budget": "budget",
    "city planning": "urban planning", "neighborhood life": "urban planning",
    "quality of life": "urban planning", "infrastructure": "urban planning",
    "education": "education",
    "public health": "public health", "mental health": "public health",
    "economics": "economy", "inequality": "economy",
    "technology": "technology",
    "politics": "politics & government", "city government": "politics & government",
    "government operations": "politics & government", "corruption": "politics & government",
    "race": "race & equity",
    "culture": "culture", "history": "culture",
}

# Email domains that indicate the person works in journalism/media.
MEDIA_DOMAINS = {
    "nytimes.com", "wsj.com", "washingtonpost.com", "theatlantic.com", "newyorker.com",
    "nymag.com", "vox.com", "axios.com", "politico.com", "bloomberg.net", "reuters.com",
    "apnews.com", "npr.org", "wnyc.org", "gothamist.com", "thecity.nyc", "hellgatenyc.com",
    "nydailynews.com", "nypost.com", "amny.com", "cityandstateny.com", "citylimits.org",
    "documentedny.com", "themarshallproject.org", "propublica.org", "chalkbeat.org",
    "the74million.org", "brooklyneagle.com", "gothamgazette.com", "thenation.com",
    "motherjones.com", "slate.com", "theguardian.com", "cnn.com", "cbsnews.com",
    "ny1.com", "pix11.com", "news12.com", "abc.com", "nbcuni.com", "spectrumnews.org",
    "epicenter-nyc.com", "thecity.org", "qns.com", "observer.com", "crainsnewyork.com",
}


# Curated email-domain -> institution names (high confidence).
INST_DOMAINS = {
    # Researched 2026-08-19 for the devoted-reader pass. Without these, the
    # acronym-ish fallbacks below turn the domain string itself into a fake
    # employer: "Ocbaacp", "Bcfny", "Appad", "Ceoworks", and osc.ny.gov -> "NY".
    "ocbaacp.org": "Onondaga County Bar Association Assigned Counsel Program",
    "bcfny.org": "Brooklyn Org (formerly Brooklyn Community Foundation)",
    "appad.org": "Appellate Advocates",
    "ceoworks.org": "Center for Employment Opportunities",
    "osc.ny.gov": "Office of the New York State Comptroller",
    "apdscorporate.com": "Orijin (formerly APDS)",
    "lemosandcrane.co.uk": "Lemos&Crane",
    "dpw.com": "Davis Polk & Wardwell",
    "citizensunionfoundation.org": "Citizens Union Foundation",
    "1235strategies.com": "1235 Strategies",
    "mccreightpartners.com": "McCreight Partners",
    "nytimes.com": "The New York Times", "wsj.com": "The Wall Street Journal",
    "washingtonpost.com": "The Washington Post", "theatlantic.com": "The Atlantic",
    "newyorker.com": "The New Yorker", "nymag.com": "New York Magazine",
    "politico.com": "POLITICO", "vox.com": "Vox", "axios.com": "Axios",
    "bloomberg.net": "Bloomberg", "bloomberg.org": "Bloomberg Philanthropies",
    "reuters.com": "Reuters", "apnews.com": "Associated Press", "npr.org": "NPR",
    "wnyc.org": "WNYC", "gothamist.com": "Gothamist", "thecity.nyc": "THE CITY",
    "hellgatenyc.com": "Hell Gate", "nydailynews.com": "New York Daily News",
    "nypost.com": "New York Post", "amny.com": "amNewYork", "cityandstateny.com": "City & State",
    "citylimits.org": "City Limits", "documentedny.com": "Documented",
    "themarshallproject.org": "The Marshall Project", "propublica.org": "ProPublica",
    "chalkbeat.org": "Chalkbeat", "thenation.com": "The Nation", "crainsnewyork.com": "Crain's New York",
    "ny1.com": "NY1", "cbsnews.com": "CBS News", "abc.com": "ABC News",
    "allrise.org": "All Rise", "counciloncj.org": "Council on Criminal Justice",
    "rand.org": "RAND Corporation", "manhattan-institute.org": "Manhattan Institute",
    "vera.org": "Vera Institute of Justice", "urban.org": "Urban Institute",
    "brookings.edu": "Brookings Institution", "cbcny.org": "Citizens Budget Commission",
    # --- more curated (high confidence) ---
    "innovatingjustice.org": "Center for Justice Innovation",
    "courtinnovation.org": "Center for Justice Innovation",
    "aclu.org": "ACLU", "nyclu.org": "NYCLU", "naacpldf.org": "NAACP Legal Defense Fund",
    "robinhood.org": "Robin Hood Foundation", "fordfoundation.org": "Ford Foundation",
    "rockefellerfoundation.org": "The Rockefeller Foundation", "macfound.org": "MacArthur Foundation",
    "carnegie.org": "Carnegie Corporation of New York", "hewlett.org": "Hewlett Foundation",
    "kresge.org": "The Kresge Foundation", "arnoldventures.org": "Arnold Ventures",
    "opensocietyfoundations.org": "Open Society Foundations", "revson.org": "Charles H. Revson Foundation",
    "nyctrust.org": "The New York Community Trust", "altman.org": "Altman Foundation",
    "pewtrusts.org": "The Pew Charitable Trusts", "jpmchase.com": "JPMorgan Chase",
    "bloomberg.com": "Bloomberg", "edelman.com": "Edelman",
    "regionalplan.org": "Regional Plan Association", "rpa.org": "Regional Plan Association",
    "citizensunion.org": "Citizens Union", "nyclass.org": "NYCLASS",
    "cssny.org": "Community Service Society", "fphnyc.org": "Fund for Public Health NYC",
    "fcny.org": "Fund for the City of New York", "nycfuture.org": "Center for an Urban Future",
    "nyu.edu": "New York University", "columbia.edu": "Columbia University",
    "law.columbia.edu": "Columbia Law School", "gc.cuny.edu": "CUNY Graduate Center",
    "jjay.cuny.edu": "John Jay College", "newschool.edu": "The New School",
    "fordham.edu": "Fordham University", "princeton.edu": "Princeton University",
    "harvard.edu": "Harvard University", "law.harvard.edu": "Harvard Law School",
    "hks.harvard.edu": "Harvard Kennedy School", "yale.edu": "Yale University",
    "mit.edu": "MIT", "stanford.edu": "Stanford University", "berkeley.edu": "UC Berkeley",
    "umich.edu": "University of Michigan", "upenn.edu": "University of Pennsylvania",
    "council.nyc.gov": "New York City Council",
    "schools.nyc.gov": "NYC Department of Education", "mtaif.org": "MTA",
    "thecity.org": "THE CITY", "gothamgazette.com": "Gotham Gazette",
    "spectrumnews.org": "Spectrum News NY1", "nybg.org": "New York Botanical Garden",
    # --- law / PR / consulting / agencies / companies seen in the data ---
    "paulweiss.com": "Paul, Weiss", "nyct.com": "New York City Transit (MTA)",
    "coned.com": "Con Edison", "berlinrosen.com": "BerlinRosen", "rubenstein.com": "Rubenstein",
    "anatgerstein.com": "Anat Gerstein", "bennettmidland.com": "Bennett Midland",
    "wxystudio.com": "WXY Studio", "mas.org": "Municipal Art Society", "fwd.us": "FWD.us",
    "chicagocred.com": "Chicago CRED", "slingshotstrat.com": "Slingshot Strategies",
    "buildmsquared.com": "M Squared", "ibo.nyc.ny.us": "NYC Independent Budget Office",
    "alleghenycounty.us": "Allegheny County", "skadden.com": "Skadden",
    "cravath.com": "Cravath", "stblaw.com": "Simpson Thacher", "wlrk.com": "Wachtell",
    "davispolk.com": "Davis Polk", "clearygottlieb.com": "Cleary Gottlieb",
    "gibsondunn.com": "Gibson Dunn", "lw.com": "Latham & Watkins",
    "kirkland.com": "Kirkland & Ellis", "sullcrom.com": "Sullivan & Cromwell",
    "mta.info": "MTA", "panynj.gov": "Port Authority of NY & NJ", "dot.nyc.gov": "NYC DOT",
    "hpd.nyc.gov": "NYC Housing Preservation & Development", "nycha.nyc.gov": "NYCHA",
    "hudson.org": "Hudson Institute", "aei.org": "American Enterprise Institute",
    "cato.org": "Cato Institute", "americanprogress.org": "Center for American Progress",
    "tcf.org": "The Century Foundation", "demos.org": "Dēmos", "epi.org": "Economic Policy Institute",
    "nrdc.org": "NRDC", "edf.org": "Environmental Defense Fund", "sierraclub.org": "Sierra Club",
    "unitedway.org": "United Way", "robinhoodfoundation.org": "Robin Hood Foundation",
    "tdf.org": "Theatre Development Fund", "trustforpublicland.org": "Trust for Public Land",
    "enterprisecommunity.org": "Enterprise Community Partners", "lisc.org": "LISC",
    "habitatnyc.org": "Habitat for Humanity NYC", "coalitionforthehomeless.org": "Coalition for the Homeless",
    "win.org": "Win", "bowery.org": "Bowery Residents' Committee",
    "robinhood.org": "Robin Hood Foundation", "tigerfoundation.org": "Tiger Foundation",
    "aecf.org": "Annie E. Casey Foundation", "stvinc.com": "STV",
    # --- 2026-06 cleanup: spell out affiliations that the SLD/.edu/.gov fallbacks
    # were rendering as ugly run-together single words (e.g. "Uchicago",
    # "Nycourts", "Cmw Newyork", "Csg"). Domain-keyed so there are no
    # output-string collisions; subdomains resolve via registrable-suffix match.
    # Universities & colleges
    "uchicago.edu": "University of Chicago", "utexas.edu": "University of Texas at Austin",
    "upenn.edu": "University of Pennsylvania", "stonybrook.edu": "Stony Brook University",
    "bankstreet.edu": "Bank Street College of Education", "brown.edu": "Brown University",
    "cornell.edu": "Cornell University", "barnard.edu": "Barnard College", "bard.edu": "Bard College",
    "northeastern.edu": "Northeastern University", "northwestern.edu": "Northwestern University",
    "rutgers.edu": "Rutgers University", "temple.edu": "Temple University",
    "pratt.edu": "Pratt Institute", "georgetown.edu": "Georgetown University",
    "emory.edu": "Emory University", "drexel.edu": "Drexel University", "duke.edu": "Duke University",
    "tufts.edu": "Tufts University", "tulane.edu": "Tulane University",
    "vanderbilt.edu": "Vanderbilt University", "wesleyan.edu": "Wesleyan University",
    "williams.edu": "Williams College", "smith.edu": "Smith College", "colby.edu": "Colby College",
    "oberlin.edu": "Oberlin College", "middlebury.edu": "Middlebury College",
    "denison.edu": "Denison University", "siena.edu": "Siena College",
    "hofstra.edu": "Hofstra University", "fordham.edu": "Fordham University",
    "buffalo.edu": "University at Buffalo", "binghamton.edu": "Binghamton University",
    "albany.edu": "University at Albany", "albanylaw.edu": "Albany Law School",
    "cortland.edu": "SUNY Cortland", "suny.edu": "State University of New York",
    "arizona.edu": "University of Arizona", "auburn.edu": "Auburn University",
    "colorado.edu": "University of Colorado Boulder", "depaul.edu": "DePaul University",
    "elmhurst.edu": "Elmhurst University", "miami.edu": "University of Miami",
    "richmond.edu": "University of Richmond", "rowan.edu": "Rowan University",
    "maine.edu": "University of Maine", "nebraska.edu": "University of Nebraska",
    "hampshire.edu": "Hampshire College", "berklee.edu": "Berklee College of Music",
    "wayne.edu": "Wayne State University", "iastate.edu": "Iowa State University",
    "latech.edu": "Louisiana Tech University", "loyno.edu": "Loyola University New Orleans",
    "conncoll.edu": "Connecticut College", "mercy.edu": "Mercy University",
    "oakland.edu": "Oakland University", "suffolk.edu": "Suffolk University",
    "stjohns.edu": "St. John's University", "wagner.edu": "Wagner College",
    "american.edu": "American University", "stanford.edu": "Stanford University",
    "berkeley.edu": "UC Berkeley", "ucdavis.edu": "UC Davis", "uci.edu": "UC Irvine",
    "ucsf.edu": "UC San Francisco", "ucla.edu": "UCLA",
    "usc.edu": "University of Southern California", "uconn.edu": "University of Connecticut",
    "umass.edu": "University of Massachusetts", "umd.edu": "University of Maryland",
    "umn.edu": "University of Minnesota", "nyls.edu": "New York Law School",
    "nyit.edu": "New York Institute of Technology", "pace.edu": "Pace University",
    "liu.edu": "Long Island University", "sva.edu": "School of Visual Arts",
    # Media
    "aarp.org": "AARP", "audacy.com": "Audacy", "forbes.com": "Forbes",
    "economist.com": "The Economist", "nationalreview.com": "National Review",
    "theguardian.com": "The Guardian", "peoplemag.com": "People",
    "nysun.com": "The New York Sun", "amsterdamnews.com": "New York Amsterdam News",
    "siadvance.com": "Staten Island Advance", "newsday.com": "Newsday",
    "politicsny.com": "PoliticsNY", "therealdeal.com": "The Real Deal",
    "thetrace.org": "The Trace", "nextcity.org": "Next City", "nysfocus.com": "New York Focus",
    "imprintnews.org": "The Imprint", "placesjournal.org": "Places Journal",
    "boltsmag.org": "Bolts", "commonwealthbeacon.org": "CommonWealth Beacon",
    "thelensnola.org": "The Lens", "texastribune.org": "The Texas Tribune",
    "stlpublicradio.org": "St. Louis Public Radio", "wypr.org": "WYPR",
    "publicnewsservice.org": "Public News Service", "solitarywatch.org": "Solitary Watch",
    "thenewhumanitarian.org": "The New Humanitarian", "schnepsmedia.com": "Schneps Media",
    "nbcuni.com": "NBCUniversal", "voxmedia.com": "Vox Media", "alm.com": "ALM",
    # Law firms
    "pbwt.com": "Patterson Belknap Webb & Tyler", "cgsh.com": "Cleary Gottlieb",
    "ropesgray.com": "Ropes & Gray", "nixonpeabody.com": "Nixon Peabody",
    "debevoise.com": "Debevoise & Plimpton", "cozen.com": "Cozen O'Connor",
    "bclplaw.com": "Bryan Cave Leighton Paisner", "fenwick.com": "Fenwick & West",
    "mclaughlinstern.com": "McLaughlin & Stern", "faegredrinker.com": "Faegre Drinker",
    "bakermckenzie.com": "Baker McKenzie", "geragos.com": "Geragos & Geragos",
    "hraadvisors.com": "HR&A Advisors",
    # Finance / companies
    "morganstanley.com": "Morgan Stanley", "barclays.com": "Barclays", "cbre.com": "CBRE",
    "compass.com": "Compass", "ngkf.com": "Newmark", "guidehouse.com": "Guidehouse",
    # Foundations
    "joycefdn.org": "The Joyce Foundation", "helmsleytrust.org": "Helmsley Charitable Trust",
    "revsonfoundation.org": "Charles H. Revson Foundation",
    "schusterman.org": "Schusterman Family Philanthropies", "towfoundation.org": "The Tow Foundation",
    "pinkertonfdn.org": "The Pinkerton Foundation", "wtgrantfdn.org": "William T. Grant Foundation",
    "unboundphilanthropy.org": "Unbound Philanthropy", "thejusttrust.org": "The Just Trust",
    "nyhealthfoundation.org": "New York Health Foundation",
    "thenytrust.org": "The New York Community Trust", "hollyhockfoundation.org": "Hollyhock Foundation",
    "philanthropynewyork.org": "Philanthropy New York", "pegsfoundation.org": "Peg's Foundation",
    "langeloth.org": "The Langeloth Foundation", "greenlightfund.org": "GreenLight Fund",
    "hfg.org": "Harry Frank Guggenheim Foundation", "rwjf.org": "Robert Wood Johnson Foundation",
    "csgv.org": "Coalition to Stop Gun Violence", "clarkest.com": "Clark Foundation",
    # Government (courts, city/county, federal)
    "nycourts.gov": "New York State Courts", "courts.state.ny.us": "New York State Courts",
    "nysenate.gov": "New York State Senate", "exec.ny.gov": "New York State Executive Chamber",
    "comptroller.nyc.gov": "NYC Comptroller's Office", "advocate.nyc.gov": "NYC Public Advocate's Office",
    # NYC agencies & offices — full names read better in the contact table than the
    # auto-acronym fallback, and fix the ones it would mash (bronxda, cityhall, law).
    "law.nyc.gov": "NYC Law Department", "cityhall.nyc.gov": "Office of the Mayor",
    "mocj.nyc.gov": "Mayor's Office of Criminal Justice",
    "dany.nyc.gov": "Manhattan District Attorney's Office",
    "bronxda.nyc.gov": "Bronx District Attorney's Office",
    "brooklynda.nyc.gov": "Brooklyn District Attorney's Office",
    "queensda.nyc.gov": "Queens District Attorney's Office",
    "health.nyc.gov": "NYC Department of Health and Mental Hygiene",
    "dohmh.nyc.gov": "NYC Department of Health and Mental Hygiene",
    "planning.nyc.gov": "NYC Department of City Planning",
    "dss.nyc.gov": "NYC Department of Social Services",
    "hra.nyc.gov": "NYC Human Resources Administration",
    "acs.nyc.gov": "NYC Administration for Children's Services",
    "dycd.nyc.gov": "NYC Department of Youth and Community Development",
    "aging.nyc.gov": "NYC Department for the Aging", "dfta.nyc.gov": "NYC Department for the Aging",
    "ibo.nyc.gov": "NYC Independent Budget Office",
    "ccrb.nyc.gov": "NYC Civilian Complaint Review Board",
    "doc.nyc.gov": "NYC Department of Correction",
    "probation.nyc.gov": "NYC Department of Probation",
    "sbs.nyc.gov": "NYC Small Business Services",
    "lpc.nyc.gov": "NYC Landmarks Preservation Commission",
    "dsny.nyc.gov": "NYC Department of Sanitation",
    "oti.nyc.gov": "NYC Office of Technology and Innovation",
    "parks.nyc.gov": "NYC Parks", "dob.nyc.gov": "NYC Department of Buildings",
    "dof.nyc.gov": "NYC Department of Finance", "dep.nyc.gov": "NYC Department of Environmental Protection",
    "dca.nyc.gov": "NYC Department of Consumer and Worker Protection",
    "dcwp.nyc.gov": "NYC Department of Consumer and Worker Protection",
    "dcla.nyc.gov": "NYC Department of Cultural Affairs",
    "edc.nyc": "NYC Economic Development Corporation",
    "correctionalassociation.org": "Correctional Association of New York",
    "ny.frb.org": "Federal Reserve Bank of New York", "newyorkfed.org": "Federal Reserve Bank of New York",
    "boston.gov": "City of Boston", "nashville.gov": "Metro Nashville", "phoenix.gov": "City of Phoenix",
    "austintexas.gov": "City of Austin", "baltimorecity.gov": "City of Baltimore",
    "cityofchicago.org": "City of Chicago", "cityofboise.org": "City of Boise",
    "detroitmi.gov": "City of Detroit", "durhamnc.gov": "City of Durham",
    "lacity.org": "City of Los Angeles", "sarasotafl.gov": "City of Sarasota",
    "tucsonaz.gov": "City of Tucson", "lowellma.gov": "City of Lowell",
    "newrochelleny.gov": "City of New Rochelle", "kingcounty.gov": "King County",
    "cookcountyil.gov": "Cook County", "dallascounty.org": "Dallas County",
    "harriscountytx.gov": "Harris County", "westchestercountyny.gov": "Westchester County",
    "usdoj.gov": "US Department of Justice", "usaid.gov": "US Agency for International Development",
    # NYC nonprofits, defenders, libraries, parks, culture
    "nychhc.org": "NYC Health + Hospitals", "nycja.org": "NYC Criminal Justice Agency",
    "osborneny.org": "Osborne Association", "pfnyc.org": "Partnership for New York City",
    "abny.org": "Association for a Better New York", "vitalcitynyc.org": "Vital City",
    "nycbar.org": "New York City Bar Association", "nycds.org": "New York County Defender Services",
    "nylpi.org": "New York Lawyers for the Public Interest", "lsnyc.org": "Legal Services NYC",
    "brooklynda.org": "Brooklyn District Attorney's Office", "queensda.org": "Queens District Attorney's Office",
    "nypti.org": "New York Prosecutors Training Institute", "nypl.org": "New York Public Library",
    "bklynlibrary.org": "Brooklyn Public Library", "nypublicradio.org": "New York Public Radio",
    "foodbanknyc.org": "Food Bank For New York City", "fortunesociety.org": "The Fortune Society",
    "childrensaidnyc.org": "Children's Aid", "cccnewyork.org": "Citizens' Committee for Children of New York",
    "cnycn.org": "Center for NYC Neighborhoods", "citizensnyc.org": "Citizens Committee for New York City",
    "nyfoundling.org": "The New York Foundling", "safehorizon.org": "Safe Horizon",
    "sffny.org": "Sanctuary for Families", "cucs.org": "Center for Urban Community Services",
    "riseboro.org": "RiseBoro Community Partnership", "bronxworks.org": "BronxWorks",
    "urbanupbound.org": "Urban Upbound", "greenwichhouse.org": "Greenwich House",
    "hudsonguild.org": "Hudson Guild", "gnyha.org": "Greater New York Hospital Association",
    "hanyc.org": "Hotel Association of New York City", "hanys.org": "Healthcare Association of New York State",
    "nyaprs.org": "New York Association of Psychiatric Rehabilitation Services",
    "cssny.org": "Community Service Society", "transalt.org": "Transportation Alternatives",
    "streetsblog.org": "Streetsblog", "ridersalliance.org": "Riders Alliance",
    "ny4p.org": "New Yorkers for Parks", "weact.org": "WE ACT for Environmental Justice",
    "prospectpark.org": "Prospect Park Alliance", "centralparknyc.org": "Central Park Conservancy",
    "washingtonsqpark.org": "Washington Square Park Conservancy", "thehighline.org": "The High Line",
    "timessquarenyc.org": "Times Square Alliance", "downtownny.com": "Downtown Alliance",
    "trinitywallstreet.org": "Trinity Church Wall Street", "lincolncenter.org": "Lincoln Center",
    "publictheater.org": "The Public Theater", "brooklynmuseum.org": "Brooklyn Museum",
    "fountainhouse.org": "Fountain House", "legal-aid.org": "The Legal Aid Society",
    "bds.org": "Brooklyn Defender Services", "bronxdefenders.org": "The Bronx Defenders",
    "neighborhooddefender.org": "Neighborhood Defender Service of Harlem",
    # National research / advocacy orgs
    "ncja.org": "National Criminal Justice Association",
    "ncjfcj.org": "National Council of Juvenile and Family Court Judges",
    "theiacp.org": "International Association of Chiefs of Police", "naadac.org": "NAADAC",
    "rti.org": "RTI International", "norc.org": "NORC at the University of Chicago",
    "mdrc.org": "MDRC", "cna.org": "CNA", "lac.org": "Legal Action Center",
    "doe.org": "The Doe Fund", "edc.nyc": "NYC Economic Development Corporation",
    "tpl.org": "Trust for Public Land", "cssp.org": "Center for the Study of Social Policy",
    "prrac.org": "PRRAC", "nmic.org": "Northern Manhattan Improvement Corporation",
    "iadb.org": "Inter-American Development Bank", "seiu.org": "SEIU", "fpwa.org": "FPWA",
    "csg.org": "The Council of State Governments", "cfrny.org": "Center for Family Representation",
    "brac.org": "BRAC", "ideas42.org": "ideas42", "recidiviz.org": "Recidiviz",
    "measuresforjustice.org": "Measures for Justice", "socialfinance.org": "Social Finance",
    "policingequity.org": "Center for Policing Equity", "sentencingproject.org": "The Sentencing Project",
    "innocenceproject.org": "Innocence Project", "prisonpolicy.org": "Prison Policy Initiative",
    "drugpolicy.org": "Drug Policy Alliance", "earthjustice.org": "Earthjustice",
    "giffords.org": "Giffords", "everytown.org": "Everytown for Gun Safety",
    "povertyactionlab.org": "J-PAL", "codeforamerica.org": "Code for America", "nacto.org": "NACTO",
    # Grammatical-only fix (no expansion asserted — just casing/spacing):
    "cmw-newyork.com": "CMW New York",
}
WEBMAIL = {"gmail.com","googlemail.com","yahoo.com","ymail.com","hotmail.com","outlook.com",
 "live.com","msn.com","aol.com","icloud.com","me.com","mac.com","proton.me","protonmail.com",
 "pm.me","gmx.com","fastmail.com","comcast.net","verizon.net","att.net","sbcglobal.net","idt.net",
 "optimum.net","rcn.com","earthlink.net","mindspring.com","nyc.rr.com","mail.com","ms.com","aim.com",
 "yahoo.co.uk","hotmail.co.uk","yahoo.co.jp","yahoo.ca","yahoo.de","yahoo.es","yahoo.fr","yahoo.com.au",
 "web.de","gmx.de","gmx.net","bellsouth.net","optonline.net","rocketmail.com","cox.net","shaw.ca",
 "charter.net","docomo.ne.jp","telus.net","btinternet.com","sky.com","hey.com","duck.com","myyahoo.com",
 "ymail.co.uk","googlemail.co.uk","outlook.co.uk","live.co.uk","icloud.co.uk","ntlworld.com","talktalk.net",
 "frontier.com","windstream.net","roadrunner.com","ptd.net","juno.com","netzero.net",
 # 2026-06 cleanup: more webmail/ISP variants + forwarders/feed-readers/test
 # domains the shared-domain fallback was mislabeling as institutions
 # ("Hotmail", "Bigpond", "Testform", etc.). These should yield NO affiliation.
 "126.com","bigpond.com","bigpond.net.au","bell.net","buckeye-express.com","compuserve.com",
 "ezweb.ne.jp","freenet.de","gmsil.com","gmx.ch","gmx.net","hotmail.co.jp","hotmail.fr",
 "hotmail.ca","iinet.net.au","libero.it","live.fr","mailbox.org","mecoinbox.com","mozmail.com",
 "mt-system.ru","naver.com","net-lix.de","obox.co.za","pacbell.net","passinbox.com","pipeline.com",
 "pobox.com","rogers.com","simplelogin.com","t-online.de","tin.it","yahoo.com.hk","yandex.ru",
 "zoominternet.net","testform.xyz","ino.to","feed.readwise.io","feedb.in","feedly.email",
 "kill-the-newsletter.com","knology.net",
 "hotmail.de","hotmail.es","hotmail.it","hotmail.com.mx","hotmail.com.br","live.de","live.it",
 "live.es","outlook.de","outlook.es","outlook.fr","outlook.it","yahoo.de","yahoo.es","yahoo.it",
 "yahoo.com.br","yahoo.com.mx","yahoo.co.in","yahoo.com.ar"}


DOMCOUNT = {}   # email-domain -> # of distinct people using it (filled in main; for the shared-domain fallback)
SUB_INFO = {}   # email -> {"ghost_active": bool (Ghost flag), "seen": last_seen ISO, "created": Ghost signup date}
MC_SUB = set()      # emails Mailchimp lists as status=subscribed — the system of record for the newsletter
MC_UNSUB = {}       # email -> Mailchimp unsubscribe date. Unsub wins UNLESS the Ghost signup is newer (resubscribe).


def _curated_inst(dom):
    """Look up a domain in the curated map, trying the exact domain first and
    then its registrable form, so subdomains resolve (sas.upenn.edu ->
    upenn.edu, austin.utexas.edu -> utexas.edu, law.northwestern.edu ->
    northwestern.edu). Exact entries (e.g. law.columbia.edu) still win."""
    if dom in INST_DOMAINS:
        return INST_DOMAINS[dom]
    parts = dom.split(".")
    for n in (2, 3):
        if len(parts) > n:
            cand = ".".join(parts[-n:])
            if cand in INST_DOMAINS:
                return INST_DOMAINS[cand]
    return None


def _naive_inst(dom):
    """What the pre-curation fallback would have produced for this domain (.edu/
    .gov root, hyphen-split, .org SLD, shared-domain SLD). Used to recognize a
    stored institution as a stale MACHINE value (e.g. 'Uchicago', 'Nycourts',
    'Vitalcitynyc') that is safe to refresh to the curated spelling — without
    touching human-curated values like 'Columbia Law School' or 'NYU Wagner',
    which never equal this naive form."""
    if dom in WEBMAIL:
        return None
    if dom.endswith(".edu"):
        root = dom[:-4].split(".")[-1]
        return root.upper() if len(root) <= 4 else root.capitalize()
    if dom.endswith(".gov") and not (dom == "nyc.gov" or dom.endswith(".nyc.gov")):
        root = dom[:-4].split(".")[-1]
        return root.upper() if len(root) <= 5 else root.capitalize()
    sld = dom.split(".")[0]
    if "-" in sld and len(sld) >= 5:
        return " ".join(w.capitalize() for w in sld.split("-"))
    if dom.endswith(".org") and re.fullmatch(r"[a-z]{4,20}", sld):
        return sld.capitalize()
    if DOMCOUNT.get(dom, 0) >= 2 and re.fullmatch(r"[a-z0-9]{3,20}", sld):
        return sld.capitalize()
    return None


def refresh_stale_inst(people):
    """Replace a stored institution with the curated spelling ONLY when it
    exactly equals the old machine-derived garble for one of the person's email
    domains (or the domain is now treated as webmail, in which case clear it).
    Human/CRM values never match the naive form, so they're left untouched. This
    heals override-carried garbles that bypass the blank-only inference path."""
    fixed = 0
    for p in people:
        cur = (p.get("inst") or "").strip()
        if not cur:
            continue
        # Upgrade a generic 'New York City government' (a machine value) to the
        # specific agency when one of the person's emails identifies it.
        if cur == "New York City government":
            for e in (p.get("emails") or []):
                if "@" not in e:
                    continue
                dom = e.split("@")[-1].strip().lower()
                better = _curated_inst(dom) or _nyc_gov_org(dom)
                if better and better != cur:
                    p["inst"] = better; fixed += 1; break
            continue
        for e in (p.get("emails") or []):
            if "@" not in e:
                continue
            dom = e.split("@")[-1].strip().lower()
            if dom in WEBMAIL and _naive_garble(dom) == cur:
                p["inst"] = ""; fixed += 1; break
            if _naive_inst(dom) == cur:
                c = _curated_inst(dom)
                if c and c != cur:
                    p["inst"] = c; fixed += 1
                break
    return fixed


def _naive_garble(dom):
    """Like _naive_inst but ignores the webmail guard — so a stored value such as
    'Hotmail' (from a webmail variant the shared-domain fallback once mislabeled)
    can be recognized and cleared."""
    sld = dom.split(".")[0]
    if "-" in sld and len(sld) >= 5:
        return " ".join(w.capitalize() for w in sld.split("-"))
    if re.fullmatch(r"[a-z0-9]{3,20}", sld):
        return sld.capitalize()
    return None


# Host labels that aren't an agency (mail relays, web hosts) — fall back to the
# generic name for these rather than coining "NYC MAIL".
NYC_SUBDOMAIN_SKIP = {"www", "www1", "www2", "mail", "email", "smtp", "mx",
                      "owa", "exchange", "m", "mobile", "portal", "my", "apps",
                      "secure", "webmail", "mailgw"}


def _nyc_gov_org(dom):
    """Specific NYC agency from an X.nyc.gov email when the agency label is
    identifiable (oath.nyc.gov -> 'NYC OATH', parks.nyc.gov -> 'NYC Parks'),
    else generic 'New York City government'. Short labels read as acronyms
    (uppercased); longer ones are title-cased. Returns None for non-nyc.gov."""
    if dom == "nyc.gov":
        return "New York City government"
    if dom.endswith(".nyc.gov"):
        parts = dom.split(".")                       # ['oath','nyc','gov'] / ['mail','dohmh','nyc','gov']
        agency = parts[-3] if len(parts) >= 3 else ""
        if agency and agency not in NYC_SUBDOMAIN_SKIP and re.fullmatch(r"[a-z]{2,}", agency):
            return "NYC " + (agency.upper() if len(agency) <= 4 else agency.capitalize())
        return "New York City government"
    return None


def infer_institution(emails):
    """Best-guess institution from an email domain. Curated map first, then
    nyc.gov agency, .gov/.edu, then hyphenated org domains. Webmail → no guess."""
    for e in emails:
        dom = e.split("@")[-1].strip().lower()
        hit = _curated_inst(dom)
        if hit:
            return hit
        g = _nyc_gov_org(dom)
        if g:
            return g
        if dom in WEBMAIL:
            continue
        if dom.endswith(".edu"):
            root = dom[:-4].split(".")[-1]
            return root.upper() if len(root) <= 4 else root.capitalize()
        if dom.endswith(".gov"):
            root = dom[:-4].split(".")[-1]
            return root.upper() if len(root) <= 5 else root.capitalize()
        sld = dom.split(".")[0]
        if "-" in sld and len(sld) >= 5:                    # e.g. court-innovation -> Court Innovation
            return " ".join(w.capitalize() for w in sld.split("-"))
        # A .org domain is almost always an organization → name it from the SLD.
        if dom.endswith(".org") and re.fullmatch(r"[a-z]{4,20}", sld):
            return sld.capitalize()
        # A domain SHARED by 2+ people is very likely an org (not a personal vanity
        # domain) → derive an institution name from the SLD even on .com/.net/etc.
        if DOMCOUNT.get(dom, 0) >= 2 and re.fullmatch(r"[a-z0-9]{3,20}", sld):
            return sld.capitalize()
    return ""


# Latin letters that NFKD+ascii would silently DROP (so Synøve != Synove). Map
# them to their conventional ASCII spelling before stripping accents.
TRANSLIT = {"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a",
            "ß": "ss", "ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "ł": "l",
            "Ł": "l", "đ": "d", "Đ": "d", "ı": "i", "œ": "oe", "Œ": "oe"}
_TRANSLIT = str.maketrans(TRANSLIT)


def norm(s):
    if not s: return ""
    s = str(s).translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()
    s = re.sub(r"\b(dr|mr|mrs|ms|prof|jr|sr|phd|md|esq)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def firstlast(s):
    t = norm(s).split()
    return f"{t[0]} {t[-1]}" if len(t) >= 2 else (t[0] if t else "")

# Common first-name nicknames -> formal form, so "Jeff Asher" the author matches
# "Jeffrey Asher" the subscriber. Bidirectional via canonicalization.
NICKNAMES = {
    "jeff": "jeffrey", "geoff": "geoffrey", "ben": "benjamin", "benji": "benjamin",
    "mike": "michael", "mick": "michael", "chris": "christopher", "dave": "david",
    "dan": "daniel", "danny": "daniel", "tom": "thomas", "tommy": "thomas",
    "rob": "robert", "bob": "robert", "bobby": "robert", "rich": "richard",
    "rick": "richard", "dick": "richard", "jim": "james", "jimmy": "james",
    "bill": "william", "will": "william", "billy": "william", "steve": "stephen",
    "matt": "matthew", "nick": "nicholas", "tony": "anthony", "alex": "alexander",
    "sam": "samuel", "greg": "gregory", "joe": "joseph", "ed": "edward",
    "ted": "edward", "andy": "andrew", "drew": "andrew", "ken": "kenneth",
    "ron": "ronald", "pat": "patrick", "cathy": "catherine", "kate": "katherine",
    "katie": "katherine", "kathy": "katherine", "liz": "elizabeth", "beth": "elizabeth",
    "betsy": "elizabeth", "sue": "susan", "jen": "jennifer", "jenny": "jennifer",
    "becky": "rebecca", "meg": "margaret", "peggy": "margaret", "abby": "abigail",
    "josh": "joshua", "zach": "zachary", "nate": "nathaniel", "gabe": "gabriel",
    "fred": "frederick", "ray": "raymond", "vince": "vincent", "cy": "cyrus",
}


def email_norm(e):
    return (e or "").strip().lower()

GENERIC = {"info","contact","hello","admin","office","press","news","mail","email","team",
 "support","editor","editors","subscriptions","membership","members","help","newsletter",
 "comms","media","outreach","development","dev","marketing","desk","general","inquiries"}

# Webmail / ISP domains — their domain root is NOT a person's surname.
PROVIDERS = {"gmail","googlemail","yahoo","ymail","rocketmail","hotmail","outlook","live",
 "msn","aol","icloud","me","mac","proton","protonmail","pm","gmx","fastmail","hey","duck",
 "comcast","verizon","att","sbcglobal","optimum","rcn","earthlink","mindspring","zoho",
 "mail","email","ms","cloud","inbox","aim"}


# Common given names — used only to detect when an email looks like last.first
# (so the guess can swap them). Lowercase. Not exhaustive; high-frequency names.
COMMON_FIRST = set("""
james john robert michael william david richard joseph thomas charles christopher
daniel matthew anthony donald mark paul steven andrew kenneth joshua kevin brian
george edward ronald timothy jason jeffrey ryan jacob gary nicholas eric jonathan
stephen larry justin scott brandon benjamin samuel gregory alexander patrick frank
raymond jack dennis jerry tyler aaron jose adam henry nathan douglas peter zachary
kyle walter ethan jeremy harold carl keith roger gerald sean austin arthur noah
lawrence jesse joe bryan billy bruce ralph roy eugene wayne alan juan luis martin
mary patricia jennifer linda elizabeth barbara susan jessica sarah karen nancy lisa
margaret betty sandra ashley dorothy kimberly emily donna michelle carol amanda
melissa deborah stephanie rebecca laura sharon cynthia kathleen amy shirley angela
helen anna brenda pamela nicole ruth katherine virginia catherine christine samantha
debra janet rachel carolyn emma maria heather diane julie joyce victoria kelly
christina joan evelyn lauren judith megan andrea cheryl hannah jacqueline martha
gloria teresa ann sara madison frances kathryn janice jean abigail alice julia judy
sophia grace denise amber danielle marilyn beverly charlotte natalie theresa diana
allison alison alexis tracy josephine alexandra rose anne erin claire molly leah
naomi ellen jane jeremy josh ben dan tom chris dave mike steve matt nick tony greg
ed ken ron pat sam gabe nate josh zach jeff geoff vince cyrus harry erroll errol
""".split())


def name_from_email(e):
    """Best-guess display name from an email address. These are educated guesses
    (shown in gray in the UI), never treated as authoritative.
      jane.doe@x.com   -> Jane Doe        (split local part)
      aaron@naparstek.com -> Aaron Naparstek  (personal-domain surname)
      jsmith@gmail.com -> Jsmith           (single token, provider domain)
    Returns "" when nothing reasonable can be derived.
    """
    e = email_norm(e)
    if "@" not in e:
        return ""
    local, domain = e.split("@", 1)
    local = local.split("+")[0]
    parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[._\-]+", local)]
    parts = [p for p in parts if p and p not in GENERIC and len(p) >= 2]
    if len(parts) >= 2:
        # First token + LAST token (not the middle): jane.marie.doe -> Jane Doe.
        first, last = parts[0], parts[-1]
        # If the surname slot is a common given name and the first slot isn't, the
        # email is likely last.first — swap (e.g. "smith.allison" -> Allison Smith).
        if last in COMMON_FIRST and first not in COMMON_FIRST:
            first, last = last, first
        return f"{first.capitalize()} {last.capitalize()}"
    if len(parts) == 1:
        first = parts[0]
        droot = domain.split(".")[0]
        tld = domain.rsplit(".", 1)[-1]
        # Personal/vanity domain → use the domain root as a likely surname.
        if (3 <= len(first) <= 11 and droot not in PROVIDERS and droot != first
                and re.fullmatch(r"[a-z]{4,12}", droot)
                and tld in ("com", "net", "co", "io")
                and domain not in INST_DOMAINS          # don't use a known org domain as a surname
                and not domain.endswith((".edu", ".gov"))):
            return f"{first.capitalize()} {droot.capitalize()}"
        return first.capitalize()
    return ""


def prettify_name(name):
    """Capitalize names entered all-lowercase or ALL-CAPS (deirdre hamill ->
    Deirdre Hamill; SAM SCHWARTZ -> Sam Schwartz). Names already in mixed case
    are assumed intentional (VanNostrand, McDonnell, DeFabbia-Kane) and kept."""
    if not name or not any(c.isalpha() for c in name):
        return name
    if name != name.lower() and name != name.upper():
        return name
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), name)


def clean_name(name):
    """Strip leading/trailing junk (asterisks, stray punctuation, symbols) while
    keeping letters, digits, periods, parens, hyphens and apostrophes."""
    if not name:
        return name
    junk = r"[\s\*\|/\\_#~:;,\"'<>\[\]{}!?@^&+=]"
    name = re.sub(r"^" + junk + r"+", "", name)
    name = re.sub(junk + r"+$", "", name)
    return name.strip()


def primary_email(emails):
    """First real address; a made-up @vitalcitynyc.org one is last resort."""
    reals = [e for e in emails if not e.endswith("vitalcitynyc.org")]
    return reals[0] if reals else (emails[0] if emails else "")


def set_email(p, email):
    """Add an email to the person's list (a person can have several) and keep
    `e` as the primary, preferring a real address over a @vitalcitynyc.org one."""
    email = email_norm(email)
    if not email:
        return
    if email not in p["emails"]:
        p["emails"].append(email)
    p["e"] = primary_email(p["emails"])


def set_name(p, name, given):
    """Set a person's display name, tracking whether it's authoritative ('given')
    or an email guess ('guess'). A given name upgrades a previous guess."""
    name = prettify_name(clean_name(name))
    if not name:
        return
    if not p["n"]:
        p["n"], p["ns"] = name, "given" if given else "guess"
    elif given and p.get("ns") == "guess":
        p["n"], p["ns"] = name, "given"


def _set(v):
    """A category column counts as 'set' for any truthy, non-empty value
    (1, x, yes, TRUE, etc.) — tolerant of however the team marks the sheet."""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "0", "no", "false", "n", "-")


def _crm_rows():
    """Yield (header_list, row_list) from the contacts source. Prefers a CSV
    export of the maintained Google Sheet (private/contacts_source.csv); falls
    back to the original Excel agglomeration (sheet 'combined')."""
    csv_path = PRIV / "contacts_source.csv"
    if csv_path.exists():
        with open(csv_path, newline="") as f:
            rows = list(csv.reader(f))
        return rows[0], rows[1:]
    wb = openpyxl.load_workbook(PRIV / "contacts_source.xlsx", read_only=True, data_only=True)
    ws = wb["combined"]
    rows = list(ws.iter_rows(values_only=True))
    return [str(c) for c in rows[0]], rows[1:]


def load_crm():
    hdr, rows = _crm_rows()
    idx = {str(h).strip(): i for i, h in enumerate(hdr)}
    def cell(r, h):
        i = idx.get(h)
        return r[i] if (i is not None and i < len(r)) else None
    def cats_from(r, allowed):
        """Categories for a row, from either per-category boolean columns OR a
        single 'categories'/'specialties' column with a ;- or ,-separated list."""
        found = {c for c in allowed if _set(cell(r, c))}
        for col in ("categories", "specialties", "specialty", "type", "types"):
            v = cell(r, col)
            if v:
                wanted = {x.strip().lower() for x in re.split(r"[;,]", str(v)) if x.strip()}
                if "architect" in wanted: wanted.add("architecture")   # moved type -> specialty
                found |= {c for c in allowed if c.lower() in wanted}
        return sorted(found)

    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        out.append({
            "name": str(r[0]).strip(),
            "email": email_norm(cell(r, "email")),
            "institution": (cell(r, "institution") or "").strip(),
            "role": (cell(r, "role") or "").strip(),
            "types": cats_from(r, PERSON_CATS),
            "topics": cats_from(r, TOPIC_CATS),
        })
    return out


HP_RANK = {"attended": 4, "regrets": 3, "invited": 2, "considered": 1}

def hp_status(raw):
    """Collapse a vendor status to attended / regrets / invited / considered."""
    r = (raw or "").strip().lower()
    if r in ("attending", "attended", "checked in", "yes"): return "attended"
    if r in ("regrets", "declined", "no", "not attending"): return "regrets"
    if r in ("considered", "consider", "recommended"): return "considered"
    if r in ("", "sent", "email opened", "page viewed", "bounced", "unsubscribed",
             "on the list", "invited", "no reply", "maybe"): return "invited"
    return "invited"

def merge_hp(a, b):
    """Union two guest-list histories by event, keeping the stronger status."""
    out = {h["ev"]: dict(h) for h in a if h.get("ev")}
    for h in b:
        if not h.get("ev"): continue
        cur = out.get(h["ev"])
        if cur is None or HP_RANK.get(h["status"], 0) > HP_RANK.get(cur["status"], 0):
            out[h["ev"]] = dict(h)
    return sorted(out.values(), key=lambda h: h["ev"])


def fold(q, p):
    """Fold person p into person q (q is kept). Combines flags, sums giving,
    unions categories/emails, prefers a real email and a confirmed name."""
    q["mem"], q["auth"], q["don"] = q["mem"] or p["mem"], q["auth"] or p["auth"], q["don"] or p["don"]
    q["unsub"] = q["unsub"] or p["unsub"]
    q["arts"] = max(q["arts"], p["arts"])
    q["alast"] = max(q.get("alast", ""), p.get("alast", ""))   # most recent VC contribution date
    q["hp"] = merge_hp(q.get("hp") or [], p.get("hp") or [])
    q["senior"] = q.get("senior") or p.get("senior") or 0
    q["damt"] = round(q["damt"] + p["damt"], 2)
    q["dcnt"] += p["dcnt"]
    q["d7"] = round(q["d7"] + p["d7"], 2); q["d7c"] += p["d7c"]
    q["d30"] = round(q["d30"] + p["d30"], 2); q["d30c"] += p["d30c"]
    if p["udate"] > q["udate"]: q["udate"] = p["udate"]
    q["erate"]=max(q["erate"],p["erate"]); q["eopen"]=max(q["eopen"],p["eopen"]); q["eclick"]=max(q["eclick"],p["eclick"])
    q["types"] = sorted(set(q["types"]) | set(p["types"]))
    q["topics"] = sorted(set(q["topics"]) | set(p["topics"]))
    q["src"] = sorted(set(q["src"]) | set(p["src"]))
    for e in p["emails"]:
        if e not in q["emails"]:
            q["emails"].append(e)
    q["e"] = primary_email(q["emails"])
    if p["inst"] and not q["inst"]: q["inst"] = p["inst"]
    if p["role"] and not q["role"]: q["role"] = p["role"]
    if p.get("aname") and not q.get("aname"): q["aname"] = p["aname"]
    if p["since"] and (not q["since"] or p["since"] < q["since"]): q["since"] = p["since"]
    if p["dlast"] > q["dlast"]: q["dlast"] = p["dlast"]
    if q["ns"] == "guess" and p["ns"] == "given": q["n"], q["ns"] = p["n"], "given"


def merge_key(name, nick=False):
    """first|last merge key from a name: drops middle names/initials and (with
    nick=True) maps nicknames to a formal form. norm() already transliterates
    accents, so 'Synøve N. Andersen' and 'Synove Andersen' share a key."""
    parts = norm(name).split()
    if len(parts) < 2:                 # single tokens never merge by name
        return None
    first, last = parts[0], parts[-1]
    if nick:
        first = NICKNAMES.get(first, first)
    return first + "|" + last


def known(p):
    """A 'known' person carries identity beyond a bare subscription — a category,
    authorship or a gift. Used to gate the looser nickname merge so two unrelated
    subscribers ('Dan Lee'/'Daniel Lee') are never fused."""
    return bool(p["auth"] or p["don"] or p["types"])


def merge_people(people):
    """Consolidate the same person split across sources/emails. Two passes:
      1. exact key (first|last, accent- and middle-name-insensitive) — always.
      2. nickname key (Dan->Daniel) — only when at least one side is 'known',
         to keep namesake risk low.
    Within a pass, only merge when exactly one prior record shares the key."""
    def run(rows, keyfn, guard):
        seen, out = {}, []
        for p in rows:
            k = keyfn(p["n"])
            if k and k in seen and (guard is None or guard(seen[k], p)):
                fold(seen[k], p)
            else:
                if k:
                    seen.setdefault(k, p)
                out.append(p)
        return out
    people = run(people, lambda n: merge_key(n, nick=False), None)
    people = run(people, lambda n: merge_key(n, nick=True), lambda a, b: known(a) or known(b))
    return people


def load_author_file():
    """Authoritative contributor roster (Google Contacts export). Returns
    [{name, email}] using the first non-@vitalcitynyc.org email as the real one."""
    path = PRIV / "vc_authors.csv"
    if not path.exists():
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            parts = [(r.get(c) or "").strip() for c in ("First Name", "Middle Name", "Last Name", "Name Suffix")]
            name = " ".join(p for p in parts if p).strip()
            if not name:
                continue
            emails = []
            for col in ("E-mail 1 - Value", "E-mail 2 - Value", "E-mail 3 - Value"):
                for e in re.split(r":::|,", r.get(col) or ""):
                    e = e.strip().lower()
                    if "@" in e and not e.endswith("vitalcitynyc.org") and e not in emails:
                        emails.append(e)
            out.append({"name": name, "emails": emails})
    return out


def main():
    # ---- index helpers ----
    people = []                 # list of person dicts
    by_email = {}               # email -> person
    by_name = {}                # norm name -> person
    by_fl = {}                  # first+last -> person
    by_tight = {}               # space/punct-insensitive full name (so "Nancy La Vigne" == "Nancy LaVigne")

    def tight(s):
        return re.sub(r"[^a-z0-9]", "", norm(s))

    def get_or_make(emails=None, name="", fl=""):
        for e in (emails or []):
            if e and e in by_email: return by_email[e]
        if name and name in by_name: return by_name[name]
        if fl and fl in by_fl: return by_fl[fl]
        if name and len(norm(name).split()) >= 2 and tight(name) in by_tight:
            return by_tight[tight(name)]   # last resort: same letters, different spacing/punctuation
        p = {"n": "", "ns": "", "e": "", "emails": [], "inst": "", "role": "",
             "types": [], "topics": [], "mem": 0, "since": "", "auth": 0, "arts": 0,
             "aname": "", "alast": "", "don": 0, "damt": 0.0, "dcnt": 0, "dlast": "", "unsub": 0, "udate": "",
             "d7": 0.0, "d7c": 0, "d30": 0.0, "d30c": 0,
             "erate": 0, "eopen": 0, "eclick": 0, "wiki": 0,
             "press": 0, "poutlet": "", "ptw": "", "src": [], "hp": []}
        people.append(p)
        return p

    def index(p):
        for e in p["emails"]:
            by_email.setdefault(e, p)
        nn = norm(p.get("n"))
        if nn and len(nn.split()) >= 2:        # only first+last names are merge keys; single tokens match by email only
            by_name.setdefault(nn, p)
            by_fl.setdefault(firstlast(p["n"]), p)
            by_tight.setdefault(tight(p["n"]), p)

    # ---- 1. Members (the spine) ----
    members_total = 0
    with open(PRIV / "members_source.csv", newline="") as f:
        for row in csv.DictReader(f):
            members_total += 1
            email = email_norm(row.get("email"))
            recorded = (row.get("name") or "").strip()
            guess = name_from_email(email) if not recorded else ""
            p = get_or_make(emails=[email], name=norm(recorded or guess))
            p["mem"] = 1
            p["src"].append("member")
            set_email(p, email)
            set_name(p, recorded, True) if recorded else set_name(p, guess, False)
            since = (row.get("created_at") or "")[:10]
            if since and not p["since"]: p["since"] = since
            # Record which emails are *actively* subscribed in Ghost + their last activity,
            # for the resubscribe fix and for marking the subscription email(s) in the UI.
            if email:
                gactive = str(row.get("subscribed") or "").strip().lower() in ("1", "true", "yes")
                SUB_INFO[email] = {"ghost_active": gactive,
                                   "seen": (row.get("last_seen") or "").strip(),
                                   "created": since}   # Ghost signup date (for the resubscribe tiebreaker)
            index(p)

    # (Subscribers come from Ghost only — Mailchimp subscribed list intentionally
    #  not used; the two drift and mixing them caused confusion.)

    # ---- 2. CRM contacts (types) ----
    crm = load_crm()
    crm_total = len(crm)
    for c in crm:
        p = get_or_make(emails=[c["email"]], name=norm(c["name"]), fl=firstlast(c["name"]))
        set_name(p, c["name"], True)
        set_email(p, c["email"])
        if c["institution"] and not p["inst"]: p["inst"] = c["institution"]
        if c["role"] and not p["role"]: p["role"] = c["role"]
        p["types"] = sorted(set(p["types"]) | set(c["types"]))
        p["topics"] = sorted(set(p["topics"]) | set(c["topics"]))
        if "crm" not in p["src"]: p["src"].append("crm")
        index(p)

    # ---- 3. Authors (article counts + specialties inferred from their pieces) ----
    authors = json.loads((ROOT / "data" / "authors.json").read_text())
    try:
        catalogue = json.loads((ROOT / "data" / "catalogue.json").read_text())
    except Exception:
        catalogue = []
    author_specs = {}   # norm author name -> set of specialty domains they've written about
    author_last = {}    # norm author name -> most recent published_date (YYYY-MM-DD)
    for art in catalogue:
        specs = {TOPIC_MAP[t.lower()] for t in art.get("topics", []) if t.lower() in TOPIC_MAP}
        d = art.get("published_date") or ""
        for au in art.get("authors", []):
            k = norm(au)
            if specs:
                author_specs.setdefault(k, set()).update(specs)
            if d and d > author_last.get(k, ""):
                author_last[k] = d
    authors_total = 0
    for a in authors:
        nn = norm(a["name"])
        if not nn or nn in NONPERSON: continue
        authors_total += 1
        p = get_or_make(name=nn, fl=firstlast(a["name"]))
        set_name(p, a["name"], True)
        p["auth"] = 1
        p["arts"] = a.get("post_count", 0)
        if not p.get("aname"): p["aname"] = a["name"]   # exact catalogue byline, for deep-linking
        if author_last.get(nn, "") > p.get("alast", ""): p["alast"] = author_last[nn]   # latest VC piece date
        p["types"] = sorted(set(p["types"]) | {"VC contributor"})   # anyone who wrote for us is a contributor
        p["topics"] = sorted(set(p["topics"]) | author_specs.get(nn, set()))
        if "author" not in p["src"]: p["src"].append("author")
        index(p)

    # ---- 3b. Authoritative contributor roster (real emails) ----
    roster = load_author_file()
    for a in roster:
        nn = norm(a["name"])
        if not nn or nn in NONPERSON:
            continue
        p = get_or_make(emails=a["emails"], name=nn, fl=firstlast(a["name"]))
        set_name(p, a["name"], True)
        for e in a["emails"]:
            set_email(p, e)
        p["types"] = sorted(set(p["types"]) | {"VC contributor"})
        p["auth"] = 1
        if author_last.get(nn, "") > p.get("alast", ""): p["alast"] = author_last[nn]
        p["topics"] = sorted(set(p["topics"]) | author_specs.get(nn, set()))
        if "author" not in p["src"]: p["src"].append("author")
        index(p)

    # ---- 4. Donors (FCNY giving) ----
    donors_path = PRIV / "donors_source.csv"
    donors_total = 0
    if donors_path.exists():
        with open(donors_path, newline="") as f:
            for row in csv.DictReader(f):
                email = email_norm(row.get("Email"))
                fname = (row.get("First Name") or "").strip()
                lname = (row.get("Last Name") or "").strip()
                name = f"{fname} {lname}".strip()
                if not email and not name:
                    continue
                donors_total += 1
                try:
                    amt = float(row.get("Summed Donation Amount") or 0)
                except ValueError:
                    amt = 0.0
                try:
                    cnt = int(float(row.get("Donations Count") or 0))
                except ValueError:
                    cnt = 0
                p = get_or_make(emails=[email], name=norm(name), fl=firstlast(name))
                set_name(p, name, True)
                set_email(p, email)
                p["don"] = 1
                p["damt"] = round(p["damt"] + amt, 2)
                p["dcnt"] += cnt
                # recent-window giving (for the activity bar)
                try: p["d7"] = round(p["d7"] + float(row.get("Amount 7d") or 0), 2)
                except ValueError: pass
                try: p["d7c"] += int(float(row.get("Count 7d") or 0))
                except ValueError: pass
                try: p["d30"] = round(p["d30"] + float(row.get("Amount 30d") or 0), 2)
                except ValueError: pass
                try: p["d30c"] += int(float(row.get("Count 30d") or 0))
                except ValueError: pass
                # most-recent gift date (M/D/YYYY ... -> YYYY-MM-DD), keep the latest
                ld = (row.get("Last Donation at") or "").strip()
                m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", ld)
                if m:
                    iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                    if iso > p["dlast"]:
                        p["dlast"] = iso
                if "donor" not in p["src"]:
                    p["src"].append("donor")
                index(p)

    # ---- 4b2. Press contacts (curated media list) — flagged press, may also be
    #      subscribers/donors. Merges by email/name onto the existing record. ----
    press_path = PRIV / "press_source.csv"
    press_total = 0
    if press_path.exists():
        with open(press_path, newline="") as f:
            for row in csv.DictReader(f):
                email = email_norm(row.get("email"))
                name = (row.get("name") or "").strip()
                if not email and not name:
                    continue
                press_total += 1
                p = get_or_make(emails=[email], name=norm(name), fl=firstlast(name))
                set_name(p, name, True)
                if email:
                    set_email(p, email)
                p["press"] = 1
                outlet = (row.get("outlet") or "").strip()
                title  = (row.get("title") or "").strip()
                tw     = (row.get("twitter") or "").strip()
                if outlet:
                    p["poutlet"] = outlet
                    if not p["inst"]:
                        p["inst"] = outlet
                if title and not p["role"]:
                    p["role"] = title
                if tw and not p.get("ptw"):
                    p["ptw"] = tw
                p["types"] = sorted(set(p["types"]) | {"journalist"})
                if "press" not in p["src"]:
                    p["src"].append("press")
                index(p)
    print(f"press contacts: {press_total}")

    # ---- 4c. Unsubscribed (Mailchimp export) — former newsletter contacts ----
    unsub_path = PRIV / "unsubscribed_source.csv"
    if unsub_path.exists():
        with open(unsub_path, newline="") as f:
            for row in csv.DictReader(f):
                email = email_norm(row.get("Email"))
                if not email:
                    continue
                fn, ln = (row.get("First Name") or "").strip(), (row.get("Last Name") or "").strip()
                name = f"{fn} {ln}".strip()
                p = get_or_make(emails=[email], name=norm(name), fl=firstlast(name))
                set_email(p, email)
                if name:
                    set_name(p, name, True)
                p["unsub"] = 1
                ud = (row.get("Unsub Date") or "").strip()[:10]
                if ud > p["udate"]:
                    p["udate"] = ud
                MC_UNSUB[email] = max(ud, MC_UNSUB.get(email, ""))   # Mailchimp unsubscribe date for this email
                if "unsub" not in p["src"]:
                    p["src"].append("unsub")
                index(p)

    # ---- 4d. Mailchimp SUBSCRIBED list = the newsletter system of record (+ engagement). ----
    # Every email here is currently subscribed in Mailchimp. We also CREATE people who are
    # subscribed in Mailchimp but missing from Ghost (legacy/direct signups), so all subscribers
    # are captured — not just Ghost members.
    eng_path = PRIV / "engagement_source.csv"
    if eng_path.exists():
        with open(eng_path, newline="") as f:
            for row in csv.DictReader(f):
                em = email_norm(row.get("Email"))
                if not em:
                    continue
                MC_SUB.add(em)
                fn, ln = (row.get("First Name") or "").strip(), (row.get("Last Name") or "").strip()
                name = f"{fn} {ln}".strip()
                p = by_email.get(em)
                if not p:                                  # Mailchimp subscriber not yet in Ghost — add them
                    guess = name_from_email(em) if not name else ""
                    p = get_or_make(emails=[em], name=norm(name or guess), fl=firstlast(name))
                    set_email(p, em)
                    set_name(p, name, True) if name else set_name(p, guess, False)
                    if "mc-sub" not in p["src"]:
                        p["src"].append("mc-sub")
                    index(p)
                try: r = int(float(row.get("Rating") or 0))
                except ValueError: r = 0
                try: o = int(float(row.get("Open Rate") or 0))
                except ValueError: o = 0
                try: c = int(float(row.get("Click Rate") or 0))
                except ValueError: c = 0
                if r > p["erate"]: p["erate"] = r
                if o > p["eopen"]: p["eopen"] = o
                if c > p["eclick"]: p["eclick"] = c

    # ---- 6. Infer categories from email domain ----
    #   journalist     -> known news-outlet domains
    #   current nyc.gov -> an active NYC city email (anything ending nyc.gov)
    media_inferred = nycgov_inferred = 0
    for p in people:
        if not p["e"]:
            continue
        dom = p["e"].split("@")[-1].strip().lower()
        if dom in MEDIA_DOMAINS and "journalist" not in p["types"]:
            p["types"] = sorted(set(p["types"]) | {"journalist"})
            media_inferred += 1
        if (dom == "nyc.gov" or dom.endswith(".nyc.gov")) and "current nyc.gov" not in p["types"]:
            p["types"] = sorted(set(p["types"]) | {"current nyc.gov"})
            nycgov_inferred += 1

    # Count how many people use each email domain (org domains are shared; personal
    # vanity domains usually aren't) — powers the shared-domain institution fallback.
    DOMCOUNT.clear()
    for p in people:
        for dom in {e.split("@")[-1].strip().lower() for e in p["emails"] if "@" in e}:
            DOMCOUNT[dom] = DOMCOUNT.get(dom, 0) + 1

    # Finalize emails + institution:
    #  - drop made-up @vitalcitynyc.org byline emails from authors/contributors, BUT keep a
    #    @vitalcitynyc.org address that's a real subscriber (a Ghost member or Mailchimp-subscribed
    #    staff address, e.g. jgreenman@vitalcitynyc.org) — only the fake /users/ byline ones go,
    #  - recompute the primary email,
    #  - infer institution from an email domain where it's blank.
    for p in people:
        if p["auth"] or "VC contributor" in p["types"]:
            p["emails"] = [e for e in p["emails"]
                           if not e.endswith("vitalcitynyc.org") or e in MC_SUB or e in SUB_INFO]
        p["e"] = primary_email(p["emails"])
        if not p["inst"]:
            inst = infer_institution(p["emails"])
            if inst:
                p["inst"] = inst

    # ---- Force VC-contributor tag for emails in extra_contributors.csv ----
    # (catches contributors whose byline name didn't match a catalogue author,
    #  e.g. nickname variants like Bill vs William Bratton)
    extra = PRIV / "extra_contributors.csv"
    if extra.exists():
        with open(extra, newline="") as f:
            for row in csv.DictReader(f):
                em = email_norm(row.get("email"))
                if em and em in by_email:
                    by_email[em]["types"] = sorted(set(by_email[em]["types"]) | {"VC contributor"})

    # ---- 5. Manual name fixes (email -> corrected name) ----
    # Edits made in the explorer's edit mode are exported here and become
    # permanent for everyone on the next publish.
    overrides_path = PRIV / "name_overrides.csv"
    overrides_applied = 0
    if overrides_path.exists():
        with open(overrides_path, newline="") as f:
            for row in csv.DictReader(f):
                em = email_norm(row.get("email"))
                if not em or em not in by_email:
                    continue
                rec = by_email[em]
                fixed = (row.get("name") or "").strip()
                if fixed:
                    rec["n"], rec["ns"] = fixed, "given"
                    overrides_applied += 1
                # Optional columns, so researched employers and titles survive the
                # nightly rebuild too. people_overrides.json cannot be used for
                # this: the workflow overwrites that file with the Google Sheet
                # every run, so anything written there locally is lost.
                # An explicit first/last split, for names no heuristic can get
                # right — "Philipp and Susanne von Türk" is one household, not a
                # man surnamed "and Susanne von Türk".
                if (row.get("fn") or "").strip():
                    rec["fn"] = row["fn"].strip()
                if (row.get("ln") or "").strip():
                    rec["ln"] = row["ln"].strip()
                if (row.get("inst") or "").strip():
                    rec["inst"] = row["inst"].strip()
                if (row.get("role") or "").strip():
                    rec["role"] = row["role"].strip()
                # excl=1 marks someone who must never be solicited — sitting public
                # officials, chiefly. The prospects build filters on it.
                if (row.get("excl") or "").strip() in ("1", "y", "yes", "true"):
                    rec["excl"] = 1
                # seg: what kind of reader this is (senior-private, funder,
                # nonprofit, academic, media, official, student, role, peer,
                # staff). nyc: has a demonstrable New York link. Together with
                # engagement these drive the "likely prospect" filter.
                if (row.get("seg") or "").strip():
                    rec["seg"] = row["seg"].strip()
                if (row.get("nyc") or "").strip() in ("1", "y", "yes", "true"):
                    rec["nyc"] = 1
                if (row.get("oom") or "").strip() in ("1", "y", "yes", "true"):
                    rec["oom"] = 1

    # ---- 5a. Senior contributors (data/senior_contributors.json) ----
    # The roster on vitalcitynyc.org/contributors/, pulled by
    # senior_contributors.py each run. A type of its own so it is a facet, a
    # filter and a column everywhere, plus a flag for the products that want
    # just the yes/no. Matched by name; every one of the 35 has a byline.
    sc_path = ROOT / "data" / "senior_contributors.json"
    if sc_path.exists():
        sc = json.loads(sc_path.read_text()).get("people") or []
        sc_hit, sc_miss = 0, []
        for person in sc:
            nm = person.get("name", "")
            # Try the name as written, then without middle initials and suffixes
            # ("Tracey L. Meares" -> "Tracey Meares", "Morgan C. Williams Jr." ->
            # "Morgan Williams"), then first+last through the by_fl index.
            toks = [t for t in re.split(r"\s+", nm.strip()) if t]
            core = [t for t in toks if not re.fullmatch(r"[A-Z]\.?", t) and t.lower().strip(".,") not in ("jr", "sr", "ii", "iii")]
            cands = [nm, " ".join(core)]
            if len(core) >= 2: cands.append(core[0] + " " + core[-1])
            q = None
            for c in cands:
                q = by_name.get(norm(c)) or by_tight.get(tight(c))
                if q: break
            if q is None and len(core) >= 2:
                fl = norm(core[0]) + "|" + norm(core[-1])
                q = by_fl.get(fl) or by_fl.get(norm(core[0] + " " + core[-1]))
            if q is None:
                sc_miss.append(nm); continue
            q["types"] = sorted(set(q.get("types") or []) | {"Senior contributor"})
            q["senior"] = 1
            if person.get("bio") and not q.get("role"):
                q["role"] = person["bio"].removeprefix("is ").strip().rstrip(".")[:120]
            sc_hit += 1
        print(f"senior contributors: {sc_hit} of {len(sc)} matched to people"
              + (f"; unmatched: {', '.join(sc_miss)}" if sc_miss else ""), file=__import__("sys").stderr)

    # ---- 5b. House-party guest lists (private/events/*.csv) ----
    # One file per party, named YYYY-MM-<anything>.csv. Two shapes are read:
    # a Paperless Post export (Full Name / Email/Phone Number / Status / Invited)
    # and a plain one (name,email,status). Statuses collapse to four words --
    # attended, regrets, invited, considered -- so the tool can answer "has this
    # person ever been asked" without caring which vendor wrote the file.
    # "considered" is a working-list entry, not an invitation, and the page
    # keeps it visibly separate. Invitees the database has never seen are added
    # as their own records, so a guest who never subscribed still shows up.
    hp_matched = hp_added = 0
    for ev_path in sorted((PRIV / "events").glob("*.csv")) if (PRIV / "events").exists() else []:
        ev = ev_path.stem[:7]                         # YYYY-MM
        try:
            label = datetime.strptime(ev, "%Y-%m").strftime("%b %Y") + " house party"
        except ValueError:
            label = ev_path.stem + " house party"
        with open(ev_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                g = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                em = email_norm(g.get("email") or g.get("email/phone number") or "")
                name = g.get("name") or g.get("full name") or ""
                raw = g.get("status") or g.get("event_status") or ""
                invited_flag = g.get("invited", "")
                if not em or "@" not in em:
                    continue
                if "full name" in g and not raw and invited_flag not in ("1", "yes", "true"):
                    continue                          # a plus-one row, never sent an invitation
                status = hp_status(raw)
                if status is None:
                    continue
                rec = {"ev": ev, "label": label, "status": status, "raw": raw or status}
                q = by_email.get(em)
                if q is None:
                    q = {"n": name, "ns": "given" if name else "", "e": em, "emails": [em],
                         "inst": "", "role": "", "types": [], "topics": [], "mem": 0, "since": "",
                         "auth": 0, "arts": 0, "aname": "", "alast": "", "don": 0, "damt": 0.0,
                         "dcnt": 0, "dlast": "", "unsub": 0, "udate": "", "d7": 0.0, "d7c": 0,
                         "d30": 0.0, "d30c": 0, "erate": 0, "eopen": 0, "eclick": 0, "wiki": 0,
                         "press": 0, "poutlet": "", "ptw": "", "src": ["house party"], "hp": []}
                    people.append(q); by_email[em] = q; hp_added += 1
                else:
                    hp_matched += 1
                    if not (q.get("n") or "").strip() and name:
                        q["n"], q["ns"] = name, "given"   # a guest list is a confirmed name
                q["hp"] = merge_hp(q.get("hp") or [], [rec])
                if "house party" not in (q.get("src") or []):
                    q.setdefault("src", []).append("house party")
    if hp_matched or hp_added:
        print(f"house parties: {hp_matched} guests matched to existing people, {hp_added} added as new records",
              file=__import__("sys").stderr)

    # ---- consolidate duplicates: exact key, then nickname key (last) ----
    # Catches accents/middle initials (Synøve N. Andersen == Synove Andersen),
    # nicknames (Dan Garodnick == Daniel Garodnick) and contributors who
    # subscribed under a name variant (Jeff Asher == Jeffrey Asher).
    before = len(people)
    people = merge_people(people)
    print(f"merged {before - len(people)} duplicate-name records", file=__import__("sys").stderr)
    # (Subscriber status is computed authoritatively after overrides — see below.)

    # ---- apply exported in-tool edits (every-field) permanently ----
    # private/people_overrides.json: {personKey: {n, inst, emails, types, topics}}
    # personKey = primary email, else "name:<lowercased name>" (matches the UI).
    ov_path = PRIV / "people_overrides.json"
    if ov_path.exists():
        try:
            ov = json.loads(ov_path.read_text())
        except Exception:
            ov = {}
        deleted_keys = 0
        matched = set()
        for p in people:
            k = p["e"] if p["e"] in ov else ("name:" + (p["n"] or "").lower())
            o = ov.get(k)
            if not isinstance(o, dict):
                continue
            matched.add(k)
            if o.get("deleted"):
                p["_deleted"] = True       # extraneous entry removed in the tool
                deleted_keys += 1
                continue
            if o.get("n"):
                p["n"], p["ns"] = o["n"], "given"
            if o.get("fn"):
                p["fn"] = o["fn"]
            if o.get("ln"):
                p["ln"] = o["ln"]
            if "inst" in o:
                p["inst"] = o["inst"]
            if o.get("emails"):
                p["emails"] = o["emails"]
                p["e"] = primary_email(o["emails"])
            if o.get("types") is not None:
                p["types"] = o["types"]
            if o.get("topics") is not None:
                p["topics"] = o["topics"]
            if "note" in o:
                p["note"] = o["note"]
            if "star" in o:
                p["star"] = 1 if o.get("star") else 0
            # Merge overrides carry the combined status of the absorbed records, since the
            # records that held that status get deleted. These only ever turn a flag ON.
            if o.get("merged"):
                if o.get("mem"): p["mem"] = 1
                if o.get("auth"): p["auth"] = 1
                if o.get("don"): p["don"] = 1
                if o.get("damt"): p["damt"] = max(p.get("damt", 0.0), float(o["damt"]))
                if o.get("dcnt"): p["dcnt"] = max(p.get("dcnt", 0), int(o["dcnt"]))
                if o.get("arts"): p["arts"] = max(p.get("arts", 0), int(o["arts"]))
                if o.get("aname") and not p.get("aname"): p["aname"] = o["aname"]
        people = [p for p in people if not p.get("_deleted")]
        if deleted_keys:
            print(f"removed {deleted_keys} entries flagged deleted in people_overrides.json", file=__import__("sys").stderr)

        # Manually-added people (add:true overrides that matched no existing record).
        added = 0
        for k, o in ov.items():
            if k in matched or not isinstance(o, dict) or not o.get("add") or o.get("deleted"):
                continue
            emails = [email_norm(e) for e in (o.get("emails") or []) if email_norm(e)]
            name = (o.get("n") or "").strip()
            if not name and not emails:
                continue
            people.append({
                "n": name, "fn": o.get("fn") or "", "ln": o.get("ln") or "",
                "ns": "given", "e": primary_email(emails), "emails": emails,
                "inst": o.get("inst") or "", "role": "",
                "types": list(o.get("types") or []), "topics": list(o.get("topics") or []),
                "mem": 1 if o.get("mem") else 0, "since": "",
                "auth": 1 if o.get("auth") else 0, "arts": 0,
                "aname": name if o.get("auth") else "",
                "don": 1 if o.get("don") else 0, "damt": float(o.get("damt") or 0),
                "dcnt": 1 if o.get("don") else 0, "dlast": "", "unsub": 0,
                "note": o.get("note") or "", "star": 1 if o.get("star") else 0,
                "src": ["manual"], "added": True,
            })
            added += 1
        if added:
            print(f"added {added} manually-entered people from people_overrides.json", file=__import__("sys").stderr)

    # Heal stale machine-garble institutions carried by overrides/adds (which
    # bypass the blank-only inference path) — only where the stored value is the
    # exact old auto-derived form, never a human-curated one.
    refreshed = refresh_stale_inst(people)
    if refreshed:
        print(f"refreshed {refreshed} stale machine-derived institutions", file=__import__("sys").stderr)

    # A "VC contributor" must have an actual published byline (arts>0). The roster and CRM can
    # tag people who never published a piece (advisors, interview subjects, prospects) — that's
    # misleading, so strip the contributor tag when there's no counted piece in the catalogue.
    # (Manual adds keep their explicit author flag.)
    decontrib = 0
    for p in people:
        if p.get("added"):
            continue
        if p.get("arts", 0) <= 0 and (p.get("auth") or "VC contributor" in p.get("types", [])):
            p["auth"] = 0
            p["types"] = [t for t in p["types"] if t != "VC contributor"]
            p["aname"] = ""
            if "author" in p.get("src", []):
                p["src"].remove("author")
            decontrib += 1
    if decontrib:
        print(f"removed VC-contributor tag from {decontrib} entries with no published piece", file=__import__("sys").stderr)

    # ════ Subscriber status — Mailchimp is the system of record for the newsletter ════
    # An email is a current subscription if Mailchimp lists it as subscribed, OR it's a
    # Ghost-active member that Mailchimp hasn't unsubscribed — EXCEPT a Mailchimp unsubscribe
    # is overridden only when the Ghost signup is NEWER than the unsubscribe (a resubscribe,
    # whether under the same email or a new one). Mailchimp-unsubscribe otherwise wins.
    # This bridges the lag in the manual Ghost↔Mailchimp reconciliation in both directions.
    def email_subscribed(e):
        if e in MC_SUB:
            return True
        info = SUB_INFO.get(e)
        if not info or not info.get("ghost_active"):
            return False
        ud = MC_UNSUB.get(e)
        if not ud:
            return True                              # Ghost-active, never unsubscribed
        return (info.get("created") or "") > ud      # resubscribed: signed up for Ghost after unsubscribing
    revived = downgraded = 0
    for p in people:
        if p.get("added"):
            continue                                  # manual adds keep their explicit flags
        sub_e = [e for e in p["emails"] if email_subscribed(e)]
        if sub_e:
            if p.get("unsub"):
                revived += 1
            p["mem"], p["unsub"], p["udate"] = 1, 0, ""
            if "unsub" in p.get("src", []):
                p["src"].remove("unsub")
            if len(p["emails"]) > 1:                   # mark the subscription email(s) for the UI
                p["sub_emails"] = sub_e
                best = max(((SUB_INFO.get(e, {}).get("seen") or ""), e) for e in sub_e)
                if best[0]:
                    p["recent_email"] = best[1]
        elif any(e in MC_UNSUB for e in p["emails"]):
            if p.get("mem"):
                downgraded += 1
            p["mem"], p["unsub"] = 0, 1
        else:
            p["mem"] = 0                              # a contact who isn't on the newsletter
    print(f"subscribers: {sum(1 for p in people if p['mem'])} "
          f"(revived {revived} resubscribes; {downgraded} downgraded to unsubscribed per Mailchimp)",
          file=__import__("sys").stderr)

    # ---- drop people with no way to act on them ----
    # No email AND not a subscriber, author, donor or unsubscribed = just a name
    # in the contacts sheet (e.g. an official with no email). Not useful here.
    def keep(p):
        return bool(p["emails"] or p["mem"] or p["auth"] or p["don"] or p["unsub"] or p.get("added") or p.get("press"))
    dropped = [p for p in people if not keep(p)]
    people = [p for p in people if keep(p)]
    print(f"dropped {len(dropped)} no-contact-info entries", file=__import__("sys").stderr)

    # ---- influence flag (Wikipedia, from wiki_influence.py cache, keyed by name) ----
    wiki_path = PRIV / "wiki_cache.json"
    if wiki_path.exists():
        try:
            wiki = json.loads(wiki_path.read_text())
        except Exception:
            wiki = {}
        wcount = 0
        for p in people:
            w = wiki.get((p["n"] or "").strip().lower())
            if w and w.get("wiki"):
                p["wiki"] = 1
                wcount += 1
        print(f"flagged {wcount} influential (Wikipedia) people", file=__import__("sys").stderr)

    # ---- stats ----
    members = sum(1 for p in people if p["mem"])
    crm_people = sum(1 for p in people if "crm" in p["src"])
    author_people = sum(1 for p in people if p["auth"])
    typed = [p for p in people if p["types"]]
    type_matrix = {}
    for c in PERSON_CATS:
        grp = [p for p in people if c in p["types"]]
        type_matrix[c] = {"total": len(grp), "members": sum(1 for p in grp if p["mem"])}
    donors = sum(1 for p in people if p["don"])
    stats = {
        "total_people": len(people),
        "members_total_rows": members_total,
        "members": members,
        "crm_contacts": crm_people,
        "authors": author_people,
        "donors": donors,
        "donors_total_rows": donors_total,
        "total_raised": round(sum(p["damt"] for p in people), 2),
        "authors_who_are_members": sum(1 for p in people if p["auth"] and p["mem"]),
        "donors_who_are_members": sum(1 for p in people if p["don"] and p["mem"]),
        "donors_who_are_authors": sum(1 for p in people if p["don"] and p["auth"]),
        "crm_who_are_members": sum(1 for p in people if "crm" in p["src"] and p["mem"]),
        "typed_people": len(typed),
        "type_matrix": type_matrix,
    }

    # ---- flag machines before anyone ranks readers by engagement -------------
    # A 95%+ open rate paired with a 85%+ click rate is not an avid reader, it is
    # a corporate email security gateway following every link in the message to
    # scan it. Left unflagged these sit at the top of every "most engaged reader"
    # list — the ranking silently measures spam filters. Role mailboxes
    # (info@, support@) are excluded from personal engagement for the same
    # reason: nobody in particular is reading them.
    #
    # This FLAGS, it never deletes: a gateway address still belongs to a real
    # subscribing organisation. Anything ranking or soliciting on engagement
    # should filter on `gw`.
    ROLE_LOCAL = re.compile(r"^(info|support|office|admin|contact|sales|help|team|hello|"
                            r"enquiries|inquiries|mail|noreply|no-reply|donotreply)\b", re.I)
    gw_n = 0
    for q in people:
        local = (q.get("e") or "").split("@")[0]
        gateway = (q.get("eopen") or 0) >= 95 and (q.get("eclick") or 0) >= 85
        role = bool(ROLE_LOCAL.match(local))
        q["gw"] = 1 if gateway else (2 if role else 0)   # 1 = scanner, 2 = role mailbox
        if q["gw"]: gw_n += 1
    print(f"flagged {gw_n} addresses as gateway/role (excluded from engagement ranking)",
          file=__import__("sys").stderr)

    # ---- likely fundraising prospect ----------------------------------------
    # A description of fit, never a wealth estimate — the research this rests on
    # says so itself: "a partner at a four-person firm and a partner at Deloitte
    # carry the same label."
    #
    # Three things have to be true at once: someone who could plausibly give
    # (seniority, or a track record of giving here), a reason to care about THIS
    # publication (a New York link, or they read it closely), and no reason they
    # must not be asked. The exclusions are absolute and come first — a sitting
    # official is never a prospect however senior, because government ethics
    # rules restrict soliciting public servants.
    # Seniority is the point. The research distinguishes "private-sector senior"
    # from "private-sector other", and so must this: an estimator and a founder
    # are both "private sector", and only one belongs on a prospect list.
    CAPACITY_SEGS = {"senior-private", "funder"}
    NEVER_ASK = {"official", "role", "staff", "student"}
    prospects = 0
    for q in people:
        q["pros"] = 0
        q["prosw"] = ""
        if q.get("unsub") or q.get("gw") or q.get("excl"):
            continue
        seg = q.get("seg") or ""
        if seg in NEVER_ASK:
            continue
        # Out of market with no New York link: a purchasing manager in Michigan
        # reads this, which is flattering and not a prospect.
        if q.get("oom") and not q.get("nyc"):
            continue
        # An open rate and a click rate within a couple of points of each other
        # is the signature of a mail scanner clicking everything it opens. It can
        # also be a genuine reader with a tiny denominator — three sends, three
        # opens, three clicks — so this only withholds the prospect flag rather
        # than declaring the address a machine.
        o_, c_ = q.get("eopen") or 0, q.get("eclick") or 0
        if c_ >= 40 and (o_ - c_) <= 3:
            q["ratewarn"] = 1
            continue
        why, score = [], 0
        if q.get("don"):                                   # already gave: the strongest signal there is
            score += 3; why.append("donor")
        if seg in CAPACITY_SEGS:
            score += 2; why.append(seg)
        elif seg == "private":
            score += 1; why.append("private sector")       # not senior — a lead at best
        elif seg in ("nonprofit", "academic", "media", "peer"):
            score += 0                                     # worth knowing, rarely an individual gift
        if q.get("nyc"):
            score += 1; why.append("New York link")
        if (q.get("erate") or 0) >= 4 or (q.get("eclick") or 0) >= 20:
            score += 2; why.append("reads closely")
        elif (q.get("eclick") or 0) > 0 or (q.get("eopen") or 0) >= 70:
            score += 1; why.append("engaged")
        if q.get("wiki"):
            score += 1; why.append("notable")
        if q.get("auth"):
            score += 1; why.append("contributor")
        q["pros"], q["prosw"] = score, ", ".join(why)
        if score >= 4:
            prospects += 1
    print(f"likely fundraising prospects (score 4+): {prospects}", file=__import__("sys").stderr)

    PRIV.mkdir(exist_ok=True)
    (PRIV / "people.json").write_text(json.dumps(people, ensure_ascii=False, separators=(",", ":")))
    (PRIV / "network_stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"People (deconflicted): {len(people)}")
    print(f"  members: {members} | CRM contacts: {crm_people} | authors: {author_people} | donors: {donors}")
    print(f"  authors who are members: {stats['authors_who_are_members']}")
    print(f"  donors who are members: {stats['donors_who_are_members']} | donors who are authors: {stats['donors_who_are_authors']}")
    print(f"  total raised: ${stats['total_raised']:,.0f}")
    sz = (PRIV / "people.json").stat().st_size
    print(f"people.json: {sz//1024} KB")


if __name__ == "__main__":
    main()
