import csv
import logging
import pydot
import pylab
import re
import sqlite3
from copy import deepcopy

from toolbox import util
from toolbox.database import SqliteDatabase
from toolbox.singletonmixin import Singleton
from toolbox.molecule import Molecule

from pygibbs import kegg_compound, kegg_reaction
from pygibbs import kegg_enzyme
from pygibbs import kegg_errors
from pygibbs import kegg_parser
from pygibbs.thermodynamic_errors import MissingCompoundFormationEnergy
from pygibbs.kegg_reaction import Reaction
from SOAPpy import WSDL
import openbabel
    
class Kegg(Singleton):

    WSDL_URL = 'http://soap.genome.jp/KEGG.wsdl'
    INCHI_FILE = '../data/kegg/inchi.txt.gz'
    COMPOUND_ADDITIONS_FILE = '../data/kegg/kegg_additions.csv'

    def __init__(self, loadFromAPI=False):
        # default colors for pydot (used to plot modules)
        self.edge_color = "cadetblue"
        self.edge_fontcolor = "indigo"
        self.edge_coeff_fontcolor = "darkolivegreen"
        self.node_fontcolor_cofactor = "dodgerblue" 
        self.node_fontcolor = "white"
        self.node_fillcolor = "dodgerblue"
        self.font = "verdana"
        
        self.name2cid_map = {}
        self.cid2compound_map = {}
        self.rid2reaction_map = {}
        self.reaction2rid_map = {}
        self.rid2enzyme_map = {}
        self.ec2enzyme_map = {}
        self.inchi2cid_map = {}
        self.mid2rid_map = {}
        self.mid2name_map = {}
        self.cofactors2names = {}
        self.cid2bounds = {}

        self.db = SqliteDatabase('../data/public_data.sqlite')
        
        if loadFromAPI:
            self.FromAPI()
        else:
            self.FromDatabase()

    def _ReadCompoundEntries(self, s):
        entry2fields_map = kegg_parser.ParsedKeggFile.FromKeggAPI(s)
        for key in sorted(entry2fields_map.keys()):
            field_map = entry2fields_map[key]
            comp = kegg_compound.Compound.FromEntryDict(key, field_map)
            if comp is not None:
                self.cid2compound_map[comp.cid] = comp
                for name in comp.all_names:
                    self.name2cid_map[name] = comp.cid
                if comp.inchi:
                    self.inchi2cid_map[comp.inchi] = comp.cid

    def _ReadReactionEntries(self, s):
        entry2fields_map = kegg_parser.ParsedKeggFile.FromKeggAPI(s)
        for key in sorted(entry2fields_map.keys()):
            field_map = entry2fields_map[key]
            reaction = kegg_reaction.Reaction.FromEntryDict(key, field_map)
            if reaction is not None:
                self.rid2reaction_map[reaction.rid] = reaction

                # Create an inverse map of reactions to RIDs. This is useful for 
                # finding the IDs of reactions according to the formula (e.g. for NIST
                # reactions).
                if reaction not in self.reaction2rid_map:
                    self.reaction2rid_map[reaction] = reaction.rid 
            
    def _ReadModuleEntries(self, s):
        entry2fields_map = kegg_parser.ParsedKeggFile.FromKeggAPI(s)
        for key in sorted(entry2fields_map.keys()):
            try:
                field_map = entry2fields_map[key]
                mid = int(key[1:6])
                name = field_map["NAME"]
                #pathway = field_map.get("PATHWAY", "")
                self.mid2rid_map[mid] = None
                self.mid2name_map[mid] = name
                if "REACTION" in field_map:
                    try:
                        self.mid2rid_map[mid] = self.parse_module("M%05d" % mid, field_map)
                    except kegg_errors.KeggParseException as e:
                        logging.debug("M%05d cannot be parsed %s" % (mid, str(e)))
            except ValueError as e:
                logging.debug("module M%05d contains a syntax error - %s" % (mid, str(e)))
    
    def _ReadEnzymeEntries(self, s):
        entry2fields_map = kegg_parser.ParsedKeggFile.FromKeggAPI(s)
        for key in sorted(entry2fields_map.keys()):
            field_map = entry2fields_map[key]
            enz = kegg_enzyme.Enzyme.FromEntryDict(key, field_map)
            for reaction_id in enz.reactions:
                self.rid2enzyme_map[reaction_id] = enz
            if enz.ec in self.ec2enzyme_map:
                logging.error('Duplicate EC class %s' % enz.ec)
            else:
                self.ec2enzyme_map[enz.ec] = enz

    def _ReadMolEntries(self, s):
        conv = openbabel.OBConversion()
        conv.SetInAndOutFormats('mol', 'inchi')
        obmol = openbabel.OBMol()
        inchi_list = []
        for mol in re.split('M  END ?\n', s): # sometimes there is a 'space' between END and \n
            if mol == '':
                break
            conv.ReadString(obmol, mol)
            inchi = conv.WriteString(obmol).strip()
            if inchi == '':
                inchi_list.append(None)
            else:
                inchi_list.append(inchi)
        return inchi_list
            
    def FromAPI(self):
        logging.info("Connecting to KEGG using SOAP")
        serv = WSDL.Proxy(Kegg.WSDL_URL)

        max_id = 100000
        kegg_step = 100
        
        # Read all Compounds
        for i in xrange(0, max_id, kegg_step):
            logging.info("Parsing KEGG Compounds C%05d - C%05d" % (i, i+kegg_step))
            entry_id_list = ' '.join(['cpd:C%05d' % j for j in xrange(i, i+kegg_step)])
            s = serv.bget(entry_id_list)
            if s == '':
                logging.info("No compounds found in this range, stopping")
                break
            self._ReadCompoundEntries(s)

        all_explicit_cids = set()
        for cid in self.cid2compound_map.keys():
            comp = self.cid2compound(cid)
            if comp.formula and 'R' not in comp.formula and 'X' not in comp.formula:
                all_explicit_cids.add(cid)
        
        # Read all Compound molecular structures, and save as InChI
        all_explicit_cids = sorted(list(all_explicit_cids))
        #for i in xrange(0, len(all_explicit_cids), kegg_step):
        #    sub_compound_ids = all_explicit_cids[i:(i+kegg_step)]
        #    entry_id_list = ['cpd:C%05d' % j for j in sub_compound_ids]
        #    logging.info("Calculating InChIs for compounds %s - %s" %
        #                 (entry_id_list[0], entry_id_list[-1]))
        #    s = serv.bget('-f m ' + ' '.join(entry_id_list))
        #    inchi_list = self._ReadMolEntries(s)
        #    if len(inchi_list) != len(sub_compound_ids):
        #        print sub_compound_ids
        #        raise Exception("Error while converting KEGG compounds to InChIs")
        #    for i, cid in enumerate(sub_compound_ids):
        #        comp = self.cid2compound(cid)
        #        comp.inchi = inchi_list[i]
        #        if comp.inchi:
        #            self.inchi2cid_map[comp.inchi] = cid
        conv = openbabel.OBConversion()
        conv.SetInAndOutFormats('mol', 'inchi')
        obmol = openbabel.OBMol()
        for cid in all_explicit_cids:
            logging.info("Calculating InChIs for compound C%05d" % cid)
            s = serv.bget('-f m cpd:C%05d' % cid)
            conv.ReadString(obmol, s)
            inchi = conv.WriteString(obmol).strip()
            if inchi != '':
                comp = self.cid2compound(cid)
                comp.inchi = inchi
                self.inchi2cid_map[inchi] = cid
            else:
                logging.warning('Cannot find the InChI for C%05d' % cid)
          
        # Read all Reactions                      
        for i in xrange(0, max_id, kegg_step):
            logging.info("Parsing KEGG Reactions R%05d - R%05d" % (i, i+kegg_step))
            entry_id_list = ' '.join(['rn:R%05d' % j for j in xrange(i, i+kegg_step)])
            s = serv.bget(entry_id_list)
            if s == '':
                logging.info("No reactions found in this range, stopping")
                break
            self._ReadReactionEntries(s)

        # Gather all EC numbers and read the enzyme data
        all_ec_numbers = set()
        for reaction in self.rid2reaction_map.values():
            all_ec_numbers.update(reaction.ec_list)

        if '-.-.-.-' in all_ec_numbers:
            all_ec_numbers.remove('-.-.-.-')

        all_ec_numbers = sorted(list(all_ec_numbers))
        for i in xrange(0, len(all_ec_numbers), kegg_step):
            sub_ec_numbers = all_ec_numbers[i:(i+kegg_step)]
            entry_id_list = ['ec %s' % j for j in sub_ec_numbers]
            logging.info("Parsing KEGG Enzymes %s - %s" %
                         (entry_id_list[0], entry_id_list[-1]))
            s = serv.bget(' '.join(entry_id_list))
            self._ReadEnzymeEntries(s)
        
        # Read all Modules
        for i in xrange(0, max_id, kegg_step):
            logging.info("Parsing KEGG Modules M%05d - M%05d" % (i, i+kegg_step))
            entry_id_list = ' '.join(['md:M%05d' % j for j in xrange(i, i+kegg_step)])
            s = serv.bget(entry_id_list)
            if s == '':
                logging.info("No modules found in this range, stopping")
                break
            self._ReadModuleEntries(s)

        logging.info("Parsing the COFACTOR file")
        cofactor_csv = csv.DictReader(open('../data/thermodynamics/cofactors.csv', 'r'))
        for row in cofactor_csv:
            cid = int(row['cid'])
            name = row['name']
            if row['c_min']:
                min_c = float(row['c_min'])
            else:
                min_c = None
            if row['c_max']:
                max_c = float(row['c_max'])
            else:
                max_c = None

            self.cofactors2names[cid] = name
            self.cid2bounds[cid] = (min_c, max_c)

    def ToDatabase(self):
        logging.info('Writing to database %s', self.db)
        self.db.CreateTable('kegg_compound', 'cid INT, name TEXT, all_names TEXT, '
           'mass REAL, formula TEXT, inchi TEXT, num_electrons INT, from_kegg BOOL, '
           'pubchem_id INT, cas TEXT')
        for cid, comp in self.cid2compound_map.iteritems():
            self.db.Insert('kegg_compound', comp.ToDBRow())
        
        self.db.CreateTable('kegg_reaction', 'rid INT, all_names TEXT, definition TEXT, '
                            'ec_list TEXT, equation TEXT')
        for rid, reaction in self.rid2reaction_map.iteritems():
            self.db.Insert('kegg_reaction', reaction.ToDBRow(rid))
         
        self.db.CreateTable('kegg_enzyme', 'ec TEXT, all_names TEXT, title TEXT, rid_list TEXT, '
                            'substrate TEXT, product TEXT, cofactor TEXT, organism_list TEXT, '
                            'orthology_map TEXT, genes_map TEXT')
        for enz in self.ec2enzyme_map.values():
            self.db.Insert('kegg_enzyme', enz.ToDBRow())

        self.db.CreateTable('kegg_module', 'mid INT, name TEXT')
        self.db.CreateTable('kegg_mid2rid', 'mid INT, position INT, rid INT, flux REAL')
        for mid, rid_flux_list in self.mid2rid_map.iteritems():
            self.db.Insert('kegg_module', [mid, self.mid2name_map[mid]])
            if rid_flux_list:
                for i, (rid, flux) in enumerate(rid_flux_list):
                    self.db.Insert('kegg_mid2rid', [mid, i, rid, flux])

        self.db.CreateTable('kegg_cofactors', 'cid INT, name TEXT')
        for cid, name in self.cofactors2names.iteritems():
            self.db.Insert('kegg_cofactors', [cid, name])

        self.db.CreateTable('kegg_bounds', 'cid INT, c_min REAL, c_max REAL')
        for cid, (c_min, c_max) in self.cid2bounds.iteritems():
            self.db.Insert('kegg_bounds', [cid, c_min, c_max])
        
        self.db.Commit()

    def FromDatabase(self):
        logging.info('Reading KEGG from the database')

        for row_dict in self.db.DictReader('kegg_compound'):
            compound = kegg_compound.Compound.FromDBRow(row_dict)
            self.cid2compound_map[compound.cid] = compound
            if compound.name:
                self.name2cid_map[compound.name] = compound.cid
            if compound.inchi:
                self.inchi2cid_map[compound.inchi] = compound.cid
        
        for row_dict in self.db.DictReader('kegg_reaction'):
            reaction = Reaction.FromDBRow(row_dict)
            self.rid2reaction_map[reaction.rid] = reaction

        for reaction in set(self.rid2reaction_map.values()):
            self.reaction2rid_map[reaction] = reaction.rid 
           
        for row_dict in self.db.DictReader('kegg_enzyme'):
            enzyme = kegg_enzyme.Enzyme.FromDBRow(row_dict)
            for reaction_id in enzyme.reactions:
                self.rid2enzyme_map[reaction_id] = enzyme
            if enzyme.ec in self.ec2enzyme_map:
                logging.error('Duplicate EC class %s' % enzyme.ec)
            else:
                self.ec2enzyme_map[enzyme.ec] = enzyme
            
        for row_dict in self.db.DictReader('kegg_module'):
            self.mid2name_map[row_dict['mid']] = row_dict['name']
            
        for row in self.db.Execute('SELECT mid, position, rid, flux FROM kegg_mid2rid '
                              'ORDER BY mid,position'):
            mid, _position, rid, flux = row
            self.mid2rid_map.setdefault(mid, []).append((rid, flux))
        
        for row_dict in self.db.DictReader('kegg_cofactors'):
            self.cofactors2names[row_dict['cid']] = row_dict['name']

        for row_dict in self.db.DictReader('kegg_bounds'):
            self.cid2bounds[row_dict['cid']] = (row_dict['c_min'], 
                                                row_dict['c_max'])
            
        self.ReadAdditionsFile()

    def ReadAdditionsFile(self):
        logging.info("Adding compound data from %s" % self.COMPOUND_ADDITIONS_FILE)
        for row_dict in csv.DictReader(open(self.COMPOUND_ADDITIONS_FILE)):
            if row_dict['cid']:
                cid = int(row_dict['cid'])
                self.name2cid_map[row_dict['name']] = cid
                try:
                    comp = self.cid2compound(cid)
                    comp.all_names.append(row_dict['name'])
                except KeyError:
                    comp = kegg_compound.Compound(cid)
                    comp.name = row_dict['name']
                    comp.all_names = [row_dict['name']]
                    self.cid2compound_map[cid] = comp
                if row_dict['inchi']:
                    if comp.inchi:
                        raise Exception('C%0d already has an InChI: %s' % (cid, comp.inchi))
                    else:
                        comp.SetInChI(row_dict['inchi'])
                        self.inchi2cid_map[row_dict['inchi']] = cid
            elif row_dict['inchi']:
                if row_dict['inchi'] in self.inchi2cid_map:
                    raise Exception("The InChI for compound %s already exists "
                                    "in KEGG at C%05d" % (row_dict['name'],
                                    self.inchi2cid_map[row_dict['inchi']]))
                new_cid = max(self.cid2compound_map.keys() + [90000]) + 1
                comp = kegg_compound.Compound(new_cid)
                comp.SetInChI(row_dict['inchi'])
                self.inchi2cid_map[row_dict['inchi']] = new_cid
                comp.name = row_dict['name']
                comp.all_names = [row_dict['name']]
                self.cid2compound_map[new_cid] = comp
    
    def AddThermodynamicData(self, thermo, priority=1):
        for cid, comp in self.cid2compound_map.iteritems():
            try:
                comp.AddThermodynamicData(thermo.cid2PseudoisomerMap(cid),
                    priority=priority, source_string=thermo.cid2SourceString(cid))
            except MissingCompoundFormationEnergy as e:
                error_string = str(e)
                comp.SetThermodynamicError(error_string)
    
    def AddGroupVectorData(self, db, table_name):
        for cid, s_groupvec in db.Execute('SELECT cid, groupvec FROM gc_groupvector'):
            self.cid2compound(int(cid)).groupvector_string = s_groupvec
            
    def AllCompounds(self):
        """Returns all the compounds."""
        return self.cid2compound_map.values()
    
    def AllReactions(self):
        """Return all the reactions."""
        return self.rid2reaction_map.values()
    
    def AllEnzymes(self):
        """Return all the enzymes."""
        return self.ec2enzyme_map.values()

    def parse_explicit_module(self, field_map, cid_mapping):
        """
            Unlike parse_module, this method doesn't use the RIDs of the reactions in the module to understand the reactions
            but rather uses the explicit reaction given on each line as the actual reaction
        """
        rids = []
        fluxes = []
        sparse_reactions = []
        cids = []
        for line in field_map["REACTION"].split('\t'):
            for rid_clause, left_clause, right_clause, remainder in re.findall('([R,0-9]+)  ([C\s\+\d\.]+) -> ([C\s\+\d\.]+)(.*)', line):
                # TODO: we take only the first RID from the list of options in this line in the module!
                # there must be a better way to choose it!
                
                rid = rid_clause.split(',')[0]
                rid = int(rid[1:])
                flux = 1
                if remainder != "":
                    for (f) in re.findall('\(x([0-9\.]+)\)', remainder):
                        flux = float(f)
                
                reaction = Reaction.FromFormula(left_clause + " => " + right_clause)
                spr = reaction.sparse
                for old_cid, (new_cid, factor) in cid_mapping.iteritems():
                    if old_cid in spr:
                        coeff = spr[old_cid]
                        del spr[old_cid]
                        spr.setdefault(new_cid, 0)
                        spr[new_cid] = spr.get(new_cid, 0) + coeff * factor
                
                for cid in spr.keys():
                    if cid not in cids and cid != 80: # don't include H+ as a compound
                        cids.append(cid)
    
                rids.append(rid)
                fluxes.append(flux)
                sparse_reactions.append(spr)
        
        Nr = len(rids)
        Nc = len(cids)
        S = pylab.zeros((Nr, Nc))
        for r in range(Nr):
            spr = sparse_reactions[r]
            for c in range(Nc):
                S[r,c] = spr.get(cids[c], 0)
        
        return (S, rids, fluxes, cids)
            
    def parse_module(self, module_name, field_map):
        """
            Reads modules in the format provided in the KEGG database.
            Note that full reactions are not given, but rather only key compound IDs just for inferring the direction of the reaction
            relative to the notation used in the RID.
            returns a list of pairs of rids and fluxes.
        """
        rid_flux_list = []
        for (rid_clause, left_clause, right_clause) in re.findall('([R,0-9]+)  ([C\s\+\d]+) <?-> ([C\s\+\d]+)', field_map["REACTION"]):
            # TODO: we take only the first RID from the list of options in this line in the module!
            # there must be a better way to choose it!
            
            rid = rid_clause.split(',')[0]
            rid = int(rid[1:])
            if rid not in self.rid2reaction_map:
                logging.debug("module %s contains an unknown RID (R%05d)" % (module_name, rid))
                continue
            
            reaction = Reaction.FromFormula(left_clause + " => " + right_clause)
            spr_module = reaction.sparse
            spr_rid = self.rid2reaction(rid).sparse
            
            directions = []
            for cid in spr_module.keys():
                if cid not in spr_rid:
                    logging.debug("in %s:R%05d does not contain the compound C%05d" % (module_name, rid, cid))
                elif spr_module[cid] * spr_rid[cid] > 0:
                    directions.append(1)
                elif spr_module[cid] * spr_rid[cid] < 0:
                    directions.append(-1)
            
            if directions == []:
                logging.debug("in %s:R%05d could not determine the direction" % (module_name, rid))
                return None
            if -1 in directions and 1 in directions:
                logging.debug("in %s:R%05d the direction is inconsistent" % (module_name, rid))
                return None
            elif -1 in directions:
                rid_flux_list.append((rid, -1))
            else:
                rid_flux_list.append((rid, 1))
        return rid_flux_list

    def rid_flux_list_to_matrix(self, rid_flux_list):
        rids = [rid for (rid, _) in rid_flux_list]
        fluxes = [f for (_, f) in rid_flux_list]
        
        # first gather all the CIDs from all the reactions
        cids = []
        for rid in rids:
            reaction = self.rid2reaction_map[rid]
            for cid in (reaction.sparse.keys()):
                if (cid not in cids and cid != 80): # don't include H+ as a compound
                    cids.append(cid)
        
        Nr = len(rids)
        Nc = len(cids)
        S = pylab.zeros((Nr, Nc))
        for r in range(Nr):
            reaction = self.rid2reaction_map[rids[r]]
            for c in range(Nc):
                S[r,c] = reaction.sparse.get(cids[c], 0) * fluxes[r]
        
        return (S, rids, fluxes, cids)
    
    def get_module(self, mid):               
        if mid not in self.mid2rid_map:
            raise kegg_errors.KeggMissingModuleException(
                "M%05d does not exist in KEGG" % mid)
        
        rid_flux_list = self.mid2rid_map[mid]
        if rid_flux_list == None: # this module has no reactions
            raise kegg_errors.KeggMissingModuleException(
                "M%05d doesn't have any reactions in KEGG" % mid)
        if len(rid_flux_list) == 0:
            raise kegg_errors.KeggMissingModuleException(
                "M%05d doesn't have any reactions in KEGG" % mid)
        return self.rid_flux_list_to_matrix(rid_flux_list)

    def cid2compound(self, cid):
        if type(cid) == int:
            return self.cid2compound_map[cid]
        elif type(cid) == str:
            return self.cid2compound_map[int(cid[1:])]
        else:
            raise KeyError("Compound ID must be integer (e.g. 22) or string (e.g. 'C00022'), not: " + str(cid))

    def rid2reaction(self, rid):
        if (type(rid) == int):
            return self.rid2reaction_map[rid]
        elif (type(rid) == str):
            return self.rid2reaction_map[int(rid[1:])]
        else:
            raise KeyError("Reaction ID must be integer (e.g. 22) or string (e.g. 'R00022')")
    
    def reaction2rid(self, reaction):
        return self.reaction2rid_map.get(reaction, None)

    def rid2link(self, rid):
        return "http://www.genome.jp/dbget-bin/www_bget?rn:R%05d" % rid
    
    def cid2inchi(self, cid):
        comp = self.cid2compound(cid)
        if (comp == None):
            return None
        else:
            return comp.inchi
    
    def cid2smiles(self, cid):
        comp = self.cid2compound(cid)
        if (comp == None):
            return None
        else:
            return comp.get_smiles()
            
    def cid2name(self, cid):
        comp = self.cid2compound(cid)
        if not comp:
            return None
        else:
            return comp.name
        
    def cid2mol(self, cid):
        return self.cid2compound(cid).GetMolecule()

    @staticmethod
    def cid2link(cid):
        """Returns the KEGG link for this compound."""
        return kegg_compound.Compound.cid2link(cid)
        
    def cid2formula(self, cid):
        return self.cid2compound(cid).formula

    def cid2atom_bag(self, cid):
        return self.cid2compound(cid).get_atom_bag()
    
    def get_all_cids(self):
        return sorted(self.cid2compound_map.keys())

    def get_all_cids_with_inchi(self):
        cids = []
        for (cid, comp) in self.cid2compound_map.iteritems():
            if (comp.inchi != None):
                cids.append(cid)
        return sorted(cids)
    
    def get_all_names(self):
        return sorted(self.name2cid_map.keys())

    def get_all_rids(self):
        return sorted(self.rid2reaction_map.keys())
    
    def inchi2cid(self, inchi):
        return self.inchi2cid_map.get(inchi, None)
    
    def name2cid(self, compound_name, cutoff=None):
        if compound_name in self.name2cid_map:
            return self.name2cid_map[compound_name], compound_name, 0

        if cutoff:
            #matches = difflib.get_close_matches(compound_name, self.get_all_names(), 1, cutoff=cutoff)
            matches = util.get_close_matches(compound_name, self.get_all_names(), n=1, cutoff=cutoff)
            if matches:
                match, distance = matches[0]
                return self.name2cid_map[match], match, distance
            
        return None, None, None

    def add_smiles(self, name, smiles):
        comp = kegg_compound.Compound()
        comp.name = name
        comp.all_names = [name]
        comp.inchi = Molecule.Smiles2InChI(smiles)
        if comp.inchi == "":
            raise Exception("The smiles notation for compound %s could not be interpreted: %s" % (name, smiles))
        
        comp.from_kegg = False

        self.name2cid_map[name] = comp.cid
        self.cid2compound_map[comp.cid] = comp

        return comp.cid
    
    def cid2png(self, cid, filename):
        self.cid2mol(cid).draw(show=False, filename=filename)
    
    def rid2compounds(self, rid):
        r = self.rid2reaction(rid)
        compound_vector = []
        stoichiometry_vector = []
        for (cid, coeff) in r.sparse.iteritems():
            compound_vector.append(cid)
            stoichiometry_vector.append(coeff)
        
        return (stoichiometry_vector, compound_vector)

    def rid2sparse_reaction(self, rid):
        return self.rid2reaction(rid).sparse

    def rid2direction(self, rid):
        return self.rid2reaction(rid).direction

    def rid2ec_list(self, rid):
        return self.rid2reaction(rid).ec_list
    
    def rid2name(self, rid):
        try:
            return self.rid2reaction(rid).name
        except KeyError:
            return "unknown reaction"

    def cid2nH_and_charge(self, cid):
        comp = self.cid2compound(cid)
        return comp.get_nH_and_charge()

    def cid2num_electrons(self, cid):
        comp = self.cid2compound(cid)
        return comp.get_num_electrons()
    
    def get_bounds(self, cid):
        return self.cid2bounds.get(cid, (None, None))
    
    def reaction2string(self, reaction, cids=False, common_names=True):
        left = []
        right = []
        for cid, coeff in reaction.sparse.iteritems():
            if (cids and not common_names):
                compound = "C%05d" % cid
            elif (cids and common_names):
                compound = self.cid2name(cid) + "(%d)" % cid
            else:
                compound = self.cid2name(cid)
            
            if (coeff == 1):
                right.append(compound)
            elif (coeff > 0):
                right.append("%g" % coeff + " " + compound)
            elif (coeff == -1):
                left.append(compound)
            elif (coeff < 0):
                left.append("%g" % (-coeff) + " " + compound)
        
        return " + ".join(left) + " = " + " + ".join(right)
    
    def insert_data_to_db(self, cursor):
        cursor.execute("DROP TABLE IF EXISTS kegg_compound")
        cursor.execute("CREATE TABLE kegg_compound (cid INT, pubchem_id INT, mass REAL, formula TEXT, inchi TEXT, from_kegg BOOL, cas TEXT, names TEXT)")
        cursor.execute("DROP INDEX IF EXISTS kegg_compound_idx")
        cursor.execute("CREATE UNIQUE INDEX kegg_compound_idx ON kegg_compound (cid)")

        cursor.execute("DROP TABLE IF EXISTS kegg_compound_names")
        cursor.execute("CREATE TABLE kegg_compound_names (cid INT, name TEXT)")
        cursor.execute("DROP INDEX IF EXISTS kegg_compound_names_idx")
        cursor.execute("CREATE INDEX kegg_compound_names_idx ON kegg_compound_names (name)")
        
        for cid, compound in self.cid2compound_map.iteritems():
            cursor.execute("INSERT INTO kegg_compound VALUES(?,?,?,?,?,?,?,?)", \
                           (cid, compound.pubchem_id, compound.mass, compound.formula, \
                            compound.inchi, compound.from_kegg, compound.cas, ";".join(compound.all_names)))
            for name in compound.all_names:
                cursor.execute("INSERT INTO kegg_compound_names VALUES(?,?)", (cid, unicode(name)))
    
    def output_csv_files(self):
        """
            Print a CSV file containing the mass of each compound in KEGG
            Print a CSV file containing the CIDs of compounds that have CoA and/or Pi
        """
        csv_file = csv.writer(open('../res/compounds.csv', 'w'))
        element_list = Molecule.GetAllElements()
        csv_file.writerow(["cid", "EXACT MASS"] + element_list)
        for cid in self.get_all_cids():
            comp = self.cid2compound(cid)
            atom_vec = comp.get_atom_vector()
            if not atom_vec:
                continue
            csv_file.writerow([cid, comp.mass] + atom_vec)
    
        csv_file = csv.writer(open('../res/coa_pi_pairs.csv', 'w'))
        csv_file.writerow(["CID +", "CID -", "Pi(1) or CoA(2)"])
        for cid in self.get_all_cids_with_inchi():
            try:
                mol = self.cid2mol(cid)
            except kegg_errors.KeggParseException:
                continue
            if len(mol) <= 5:
                continue # ignore compounds which are too small (e.g. orthophosphate)
            smiles_pi = "P(=O)([OH,O-])[OH,O-]"
            for pgroup in mol.FindSmarts(smiles_pi):
                tmp_mol = mol.Clone()
                tmp_mol.RemoveAtoms(pgroup)
                new_inchi = tmp_mol.ToInChI()
                if new_inchi in kegg.inchi2cid_map:
                    new_cid = kegg.inchi2cid_map[new_inchi]
                    csv_file.writerow([cid, new_cid, 1])
                    logging.info("Match: %s = %s + Pi" % (self.cid2name(cid), self.cid2name(new_cid)))
            
            smiles_coa = "CC(C)(COP(O)(=O)OP(O)(=O)OCC1OC(C(O)C1OP(O)(O)=O)n2cnc3c(N)ncnc23)C(O)C(~O)~NCCC(~O)~NCCS"
            for cgroup in mol.FindSmarts(smiles_coa):
                tmp_mol = mol.Clone()
                tmp_mol.SetAtomicNum(cgroup.pop(), 8) # change S to O
                tmp_mol.RemoveAtoms(cgroup)
                tmp_mol.RemoveHydrogens()
                new_inchi = tmp_mol.ToInChI()
                if new_inchi in kegg.inchi2cid_map:
                    new_cid = kegg.inchi2cid_map[new_inchi]
                    csv_file.writerow([cid, new_cid, 2])
                    logging.info("Match: %s = %s + CoA" % (self.cid2name(cid), self.cid2name(new_cid)))
                    
    def create_compound_node(self, Gdot, cid, node_name=None):
        if (node_name == None):
            node_name = "C%05d" % cid
        node = pydot.Node(node_name)
        node.set_label('"%s"' % self.cid2name(cid))
        node.set_tooltip('"C%05d"' % cid)
        node.set_URL('"http://www.genome.jp/Fig/compound/C%05d.gif"' % cid)
        
        if (cid in self.cofactors2names):
            node.set_fontcolor(self.node_fontcolor_cofactor) # color for cofactors
            node.set_shape("none")
            node.set_fontsize("12")
            node.set_fontname(self.font)
        else:
            node.set_shape("box")
            node.set_style("filled")
            node.set_fontcolor(self.node_fontcolor) # color for non-cofcators
            node.set_fillcolor(self.node_fillcolor)
            node.set_fontsize("12")
            node.set_fontname(self.font)

        Gdot.add_node(node)
        return node

    def create_reaction_nodes(self, Gdot, rid):
        node_in = pydot.Node("R%05d in" % rid)
        node_in.set_label("")
        node_in.set_shape("point")
        node_in.set_tooltip('"-> R%05d"' % rid)
        node_in.set_color(self.edge_color)
        Gdot.add_node(node_in)

        node_out = pydot.Node("R%05d out" % rid)
        node_out.set_label("")
        node_out.set_shape("point")
        node_out.set_tooltip('"R%05d ->"' % rid)
        node_out.set_color(self.edge_color) # edge connector-point color
        Gdot.add_node(node_out)
        
        self.create_reaction_edge(Gdot, node_in, node_out, rid, arrowhead="none", arrowtail="none")

        return (node_in, node_out)

    def create_reaction_edge(self, Gdot, node_from, node_to, rid, arrowhead="none", arrowtail="none"):
        """
            Create an edge for a reaction
        """
        edge = pydot.Edge(node_from, node_to)
        edge.set_label('"R%05d"' % rid)
        edge.set_color(self.edge_color) # edge line color
        edge.set_fontcolor(self.edge_fontcolor) # edge label color
        edge.set_arrowhead(arrowhead)
        edge.set_arrowtail(arrowtail)
        edge.set_fontname(self.font)
        edge.set_fontsize("12")
        edge.set_URL('"http://www.genome.jp/Fig/reaction/R%05d.gif"' % rid)
        Gdot.add_edge(edge)
        return edge
        
    def create_small_edge(self, Gdot, node_from, node_to, coeff=1, arrowhead="none", arrowtail="none"):
        """
            Create an edge that connects a compound to the 'point' node of a reaction (in or out)
        """
        edge = pydot.Edge(node_from, node_to)
        if (coeff != 1):
            edge.set_label('"%g"' % coeff)
        edge.set_color(self.edge_color)
        edge.set_fontcolor(self.edge_coeff_fontcolor)
        edge.set_arrowhead(arrowhead)
        edge.set_arrowtail(arrowtail)
        Gdot.add_edge(edge)
        return edge
    
    def draw_module(self, mid):
        (S, rids, cids) = self.get_module(mid)
        return self.draw_pathway(S, rids, cids)
            
    def draw_pathway(self, S, rids, cids):
        Gdot = pydot.Dot()
        (_, Nc) = S.shape
        
        c_nodes = []
        for c in range(Nc):
            node_map = {}
            if (cids[c] in self.cofactors2names):
                for r in pylab.find(S[:,c] != 0): # this is a co-factor, create a new node for each reaction
                    node_map[r] = self.create_compound_node(Gdot, cids[c], "C%05d_R%05d" % (cids[c], rids[r]))
            else:
                node = self.create_compound_node(Gdot, cids[c])
                for r in pylab.find(S[:,c] != 0): # point the node_map to the same node for every reaction
                    node_map[r] = node
            c_nodes.append(node_map)
       
        for r in range(S.shape[0]):
            rid = rids[r]
            s_indices = pylab.find(S[r,:] < 0)
            p_indices = pylab.find(S[r,:] > 0)
            if (len(s_indices) == 1 and len(p_indices) == 1):
                c_s = s_indices[0]
                c_p = p_indices[0]
                if (S[r,c_s] == -1 and S[r,c_p] == 1):
                    self.create_reaction_edge(Gdot, c_nodes[c_s][r], c_nodes[c_p][r], rid=rid, arrowhead="open", arrowtail="none")
                    continue
            
            # this is not a simple 1-to-1 reaction
            (in_node, out_node) = self.create_reaction_nodes(Gdot, rid)
            for c in s_indices:
                self.create_small_edge(Gdot, c_nodes[c][r], in_node, coeff=-S[r,c], arrowhead="none")
            for c in p_indices:
                self.create_small_edge(Gdot, out_node, c_nodes[c][r], coeff=S[r,c], arrowhead="open")
        
        return Gdot

    def sparse_to_hypertext(self, sparse, show_cids=True):
        s_left = []
        s_right = []
        for cid, count in sparse.iteritems():
            if abs(count) < 0.01:
                continue
            comp = self.cid2compound(cid)
            url = comp.get_link()
            name = comp.name
            if (show_cids):
                show_string = "C%05d" % cid
                title = name
            else:
                show_string = name
                title = "C%05d" % cid
            
            if count > 0:
                if count == 1:
                    s_right.append('<a href="%s" title="%s">%s</a>' % (url, title, show_string))
                else:
                    s_right.append('%g <a href="%s" title="%s">%s</a>' % (count, url, title, show_string))
            elif count < 0:
                if (count == -1):
                    s_left.append('<a href="%s" title="%s">%s</a>' % (url, title, show_string))
                else:
                    s_left.append('%g <a href="%s" title="%s">%s</a>' % (-count, url, title, show_string))
        return ' + '.join(s_left) + ' => ' + ' + '.join(s_right)
    
    def vector_to_hypertext(self, v, cids, show_cids=True):
        sparse_reaction = {}
        for c in range(len(v)):
            sparse_reaction[cids[c]] = v[c]
        return self.sparse_to_hypertext(sparse_reaction, show_cids=show_cids)

    def write_reactions_to_html(self, html_writer, S, rids, fluxes, cids, show_cids=True):
        html_writer.write("<li>Reactions:</br><ul>\n")
        
        for r in range(S.shape[0]):
            html_writer.write('<li><a href=' + self.rid2link(rids[r]) + '>R%05d' % rids[r] + '</a>')
            html_writer.write(' : ' + self.vector_to_hypertext(S[r, :].flat, cids, show_cids=show_cids))
            html_writer.write(' (x%g)' % fluxes[r])
            html_writer.write('</li>\n')
        
        v_total = pylab.dot(pylab.matrix(fluxes), S).flat
        html_writer.write('<li><b>Total:</b>  ' + self.vector_to_hypertext(v_total, cids, show_cids=show_cids) + '</li>\n')
        html_writer.write("</ul></li>\n")
        
class KeggPathologic(object):
    def __init__(self): # CO2, HCO3-

        self.edge_color = "cadetblue"
        self.edge_fontcolor = "indigo"
        self.edge_ex_in_fontcolor = "lightcyan"
        self.edge_coeff_fontcolor = "darkolivegreen"
        self.node_fontcolor_cofactor = "dodgerblue"
        self.node_fontcolor_environment = "green"
        self.node_fontcolor = "white"
        self.node_fillcolor = "dodgerblue"
        self.font = "verdana"

        # cid2uid is a used for two purposes. One is to have canonical IDs for compounds
        # according to their INCHI labels (i.e. if two CIDs have the same INCHI, all occurences of the second
        # one will be changed to the first appearing CID). If a CID is not in the INCHI file, but is used
        # by one of the reactions, it might cause the reaction to be skipped. If compound formulas are to be
        # added using "database_updates.txt", the SETC line should appear at the top.
        self.reactions = []
        self.cofactor_reaction_list = []
        self.cofactors = set()

        kegg = Kegg.getInstance()

        inchi2compound = {}
        self.cid2compound = {}
        self.cid2atom_bag = {}
        for cid in kegg.get_all_cids():
            comp = kegg.cid2compound(cid)
            if comp.inchi is not None: # always point to the lowest CID with the same InChI
                if comp.inchi in inchi2compound:
                    self.cid2compound[cid] = inchi2compound[comp.inchi]
                else:
                    inchi2compound[comp.inchi] = comp
                    self.cid2compound[cid] = comp
            else:
                self.cid2compound[cid] = comp
            self.cid2atom_bag[cid] = comp.get_atom_bag() 
        
        for rid in kegg.get_all_rids():
            reaction = kegg.rid2reaction(rid)
            ver = self.verify(reaction)
            if ver is not None:
                logging.debug("R%05d is %s, not adding it to Pathologic" % (rid, ver))
            else:
                self.reactions += self.create_reactions("R%05d" % rid, reaction.direction, reaction.sparse, rid=rid)

    def is_specific(self, reaction):
        for cid in reaction.sparse.keys():
            atom_bag = self.cid2atom_bag.get(cid, None)
            if atom_bag == None or 'R' in atom_bag:
                # This reaction cannot be checked since there is an
                # unspecific compound
                return False
        return True
    
    def is_balanced(self, reaction, balance_water=True):
        """
            Checks if the reaction is balanced: i.e. the sum of elements is conserved (not including hydrogen atoms).
            If oxygen is not balanced, the method adds water molecules in the right amount to fix it.
        """
        atom_diff = {}
        for cid, coeff in reaction.sparse.iteritems():
            atom_bag = self.cid2atom_bag.get(cid, None)
            for atomic_number, atom_count in atom_bag.iteritems():
                new_count = atom_diff.get(atomic_number, 0)
                new_count += coeff * atom_count
                atom_diff[atomic_number] = new_count

        # ignore H and O inconsistencies
        if 'H' in atom_diff:
            del atom_diff['H']
        if balance_water:
            if 'O' in atom_diff and atom_diff['O'] != 0:
                reaction.sparse[1] = reaction.sparse.get(1, 0) - atom_diff['O']
                del atom_diff['O']
        
        return max([abs(x) for x in atom_diff.values()]) < 0.01
    
    def verify(self, reaction):
        if not self.is_specific(reaction):
            # unspecific reactions cannot be automatically checked for
            # chemical balance therefore we cannot use them here.
            return "unspecific"
        elif not self.is_balanced(reaction):
            return "unbalanced"
        elif not reaction.is_not_futile():
            return "futile"
        else:
            return None
    
    def get_compound(self, cid):
        if cid in self.cid2compound:
            return self.cid2compound[cid]
        else:
            raise KeyError("The compound C%05d is not present in the cid2compound in KeggPathologic" % cid)

    def update_database(self, fname, html_writer):
        """
            Updates the database of reactions and compounds, using the update_database file.
            Commands are: SETR, DELR, COFR, SKIP
            
            In stiochio_mode, all reactions are not touched, so only SETC, NEWC, DELC, COFR are used.
        """
        
        logging.info("updating the database using %s" % fname)
        update_file = open(fname, 'r')

        banned_reactions = set()
        banned_compounds = set()
        added_reactions = []
        
        html_writer.write('<h2>Database updates:</h2>\n')
        html_writer.write('<ul>\n')
        for line in update_file.readlines():
            if (line.find('%') != -1):
                line = line[0:line.find('%')]
            line = line.strip()
            if (line == ''):
                continue
            (command, rest) = line.split(' ', 1)
            line = rest.strip()
            
            if (command == 'SETR'):
                reaction_id, formula = line.split(':')
                try:
                    reaction = Reaction.FromFormula(formula.strip())
                except kegg_errors.KeggParseException:
                    raise Exception("Syntax error in update file: " + line)
                
                rid = int(reaction_id.strip()[1:])
                reaction.SetNames("R%05d" % rid)
                reaction.rid = rid

                rxns = self.create_reactions(reaction, weight=1)
                
                html_writer.write("<li><b>Set Reaction,</b> R%05d : %s" % (rid, 
                    reaction.to_hypertext(show_cids=True)))
                #ver = rxns[0].verify(self.cid2atom_bag)
                #if ver != None:
                #    html_writer.write(' <b>WARNING: %s' % ver)
                added_reactions += rxns
            elif (command == 'DELR'):
                rid = int(line[1:])
                html_writer.write("<li><b>Ban Reaction,</b> R%05d" % (rid))
                banned_reactions.add(rid)
            elif (command == 'DELC'):
                cid = int(line[1:])
                banned_compounds.add(cid)
                html_writer.write("<li><b>Ban Compound,</b> C%05d" % (cid))
            elif (command == 'COFR'): # cofactor
                if len(line.split()) == 1:
                    name = line.strip()
                    self.cofactors.add(name)
                    html_writer.write("<li><b>Cofactor,</b> %s" % name)
                else:
                    try:
                        reaction = Reaction.FromFormula(line.strip())
                    except kegg_errors.KeggParseException:
                        raise Exception("Syntax error in update file: " + line)
                    self.cofactor_reaction_list.append((reaction.sparse, reaction.direction))
                    self.cofactors = self.cofactors.union(reaction.get_cids())
                    html_writer.write("<li><b>Cofactor Reaction,</b> " + 
                                      reaction.to_hypertext(show_cids=True))
    
        html_writer.write('</ul>\n')
        update_file.close()

        logging.info("removing reaction which are banned or involve a banned compound")
        
        # Create a new map of RID to reactions, without the banned reactions.
        temp_reactions = []
        for r in self.reactions:
            if r.rid in banned_reactions:
                logging.debug("This reaction has been banned by its RID (R%05d): %s" % (r.rid, r.name))
            elif len(banned_compounds.intersection(r.get_cids())) > 0:
                logging.debug("This reaction has been banned by at least one of its CIDs (%s): %s" % (str(banned_compounds.intersection(r.get_cids())), r.name))
            else:
                temp_reactions.append(r)
                
        # Replace all compounds in all reactions with the primary compound
        # determined earlier by the InChI identifiers.
        temp_reactions.extend(added_reactions)
        all_reactions = []
        for r in temp_reactions:
            try:
                for cid in r.get_cids():
                    compound = self.get_compound(cid)
                    if compound.cid != cid:
                            r.replace_compound(cid, compound.cid)
            except ValueError, e:
                logging.error(e)
                continue
            
            all_reactions.append(r)
        
        self.reactions = all_reactions
        
    def create_reactions(self, reaction, weight=1):
        """Creates Reaction objects needed according to the sign of the arrow."""
        if 80 in reaction.sparse:
            del reaction.sparse[80]
        
        res = []
        if reaction.direction not in ["<=", "=>", "<=>"]:
            raise kegg_errors.KeggParseException(
                "Direction must be either =>, <= or <=>")
        if reaction.direction in ["=>", "<=>"]:
            r_forward = reaction.clone()
            r_forward.SetNames("R%05d_F" % reaction.rid)
            r_forward.weight = weight
            res.append(r_forward)
        if reaction.direction in ["<=", "<=>"]:
            r_reverse = reaction.reverse()
            r_reverse.SetNames("R%05d_F" % reaction.rid)
            r_reverse.weight = weight
            res.append(r_reverse)
        return res
    
    def add_compound(self, name, cid=None, formula=None, inchi=None):
        comp = kegg_compound.Compound()
        comp.name = name
        comp.formula = formula
        comp.inchi = inchi
        comp.from_kegg = False
        self.cid2compound[comp.cid] = comp
        return comp.cid
    
    @staticmethod
    def reverse_sparse_reaction(sparse_reaction):
        backward_reaction = {}
        for (cid, coeff) in sparse_reaction.iteritems():
            backward_reaction[cid] = -coeff
        return backward_reaction
    
    def sparse_to_hypertext(self, sparse, show_cids=True, direction='=>'):
        s_left = []
        s_right = []
        for (cid, count) in sparse.iteritems():
            comp = self.get_compound(cid)
            url = comp.get_link()
            name = comp.name
            if (show_cids):
                show_string = "C%05d" % cid
                title = name
            else:
                show_string = name
                title = "C%05d" % cid
            
            if (count > 0):
                if (count == 1):
                    s_right.append('<a href="%s" title="%s">%s</a>' % (url, title, show_string))
                else:
                    s_right.append('%d <a href="%s" title="%s">%s</a>' % (count, url, title, show_string))
            elif (count < 0):
                if (count == -1):
                    s_left.append('<a href="%s" title="%s">%s</a>' % (url, title, show_string))
                else:
                    s_left.append('%d <a href="%s" title="%s">%s</a>' % (-count, url, title, show_string))
        return ' + '.join(s_left) + ' ' + direction + ' ' + ' + '.join(s_right)

    @staticmethod
    def is_subreaction(spr1, spr2):
        """
            checks if spr1 is a sub-reaction of spr2
        """
        for cid in spr1.keys():
            if 0 < spr1[cid] <= spr2.get(cid, 0):
                continue
            elif 0 > spr1[cid] >= spr2.get(cid, 0):
                continue
            else: # the coeff in spr1 is either larger than spr2, or in the wrong direction
                return False
        # if all coefficients follow the rule, this is a sub-reaction
        return True
    
    @staticmethod
    def subtract_reaction(spr, spr_to_subtract):
        for cid in spr_to_subtract.keys():
            if (spr[cid] == spr_to_subtract[cid]):
                del spr[cid]
            else:
                spr[cid] -= spr_to_subtract[cid]
    
    @staticmethod
    def neutralize_reaction(spr, spr_to_subtract, direction="<=>"):
        if not set(spr_to_subtract.keys()).issubset(set(spr.keys())):
            return
        if direction in ["<=>", "=>"]:
            while KeggPathologic.is_subreaction(spr_to_subtract, spr):
                KeggPathologic.subtract_reaction(spr, spr_to_subtract)
        if direction in ["<=>", "<="]:
            spr_small_rev = KeggPathologic.reverse_sparse_reaction(spr_to_subtract)
            while KeggPathologic.is_subreaction(spr_small_rev, spr):
                KeggPathologic.subtract_reaction(spr, spr_small_rev)                
    
    def get_unique_cids_and_reactions(self):
        """
            Gather a set of all the CIDs (unique compound IDs) which are actually used.
            Remove reaction duplicates (i.e. have the same substrates and products,
            and store them in 'unique_reaction_map'.
        """
        logging.info("creating the Stoichiometry Matrix")
        cids = set()
        for r in self.reactions:
            for cid in r.get_cids():
                cids.add(self.get_compound(cid).cid)

        cids = list(sorted(cids))
        Ncompounds = len(cids)
        cid2index = {}
        compounds = []
        for c in xrange(Ncompounds):
            cid2index[cids[c]] = c
            compounds.append(self.get_compound(cids[c]))

        # Create the columns, name the reactions (RID) in the stoichiometric matrix
        reduced_sparse_reactions = []
        for r in self.reactions:
            
            # remove the co-factor pairs from the reaction
            spr = deepcopy(r.sparse)
            for cofr_spr, cofr_direction in self.cofactor_reaction_list:
                KeggPathologic.neutralize_reaction(spr, cofr_spr, cofr_direction)
            
            reduced_sparse_reactions.append(spr)

        Nreactions = len(self.reactions)
        f = []
        S = pylab.zeros((Ncompounds, Nreactions))

        for r in range(Nreactions):
            if self.reactions[r].weight != 0:
                f.append((r, self.reactions[r].weight))
            
            for cid, count in reduced_sparse_reactions[r].iteritems():
                comp = self.get_compound(cid)
                c = cid2index[comp.cid]
                S[c, r] = count
                        
        # S can have multiple columns which are exactly the same, because a few reactions
        # share the same reactants (with different co-factors).
        # Although this is a stoichiometric redundancy, thermodynamically this is important
        # since each version of this reaction will have different constraints.
        logging.info("the Stoichiometry matrix contains %d compounds & %d reactions" % (Ncompounds, Nreactions))
        return f, S, compounds, self.reactions

    def create_compound_node(self, Gdot, comp, node_name=None, is_cofactor=False):
        if (node_name == None):
            node_name = "C%05d" % comp.cid
        node = self.get_node(Gdot, node_name)
        node.set_label('"%s"' % comp.name)

        if (is_cofactor):
            node.set_tooltip('"C%05d"' % comp.cid)
            node.set_URL('"' + comp.get_link() + '"')
            #node.set_shape("box")
            #node.set_style("filled")
            node.set_fontcolor(self.node_fontcolor_cofactor) # color for non-cofcators
            #node.set_fillcolor(self.node_fillcolor)
            node.set_fontsize("8")
            node.set_fontname(self.font)
        else:
            node.set_tooltip('"C%05d"' % comp.cid)
            node.set_URL('"' + comp.get_link() + '"')
            node.set_shape("box")
            node.set_style("filled")
            node.set_fontcolor(self.node_fontcolor) # color for non-cofcators
            node.set_fillcolor(self.node_fillcolor)
            node.set_fontsize("12")
            node.set_fontname(self.font)

        Gdot.add_node(node)
        return node
    
    def get_node(self, Gdot, name):
        node = Gdot.get_node(name)
        if (node != []):
            return node
        else:
            return pydot.Node(name)

    def create_reaction_nodes(self, Gdot, reaction, flux=1):
        node_in = self.get_node(Gdot, "R%05d in" % reaction.rid)
        node_in.set_label("")
        node_in.set_shape("point")
        node_in.set_tooltip('"-> R%05d"' % reaction.rid)
        node_in.set_color(self.edge_color)
        Gdot.add_node(node_in)

        node_out = self.get_node(Gdot, "R%05d out" % reaction.rid)
        node_out.set_label("")
        node_out.set_shape("point")
        node_out.set_tooltip('"R%05d ->"' % reaction.rid)
        node_out.set_color(self.edge_color) # edge connector-point color
        Gdot.add_node(node_out)
        
        self.create_reaction_edge(Gdot, node_in, node_out, reaction, flux=flux, arrowhead="none", arrowtail="none")

        return (node_in, node_out)

    def create_reaction_edge(self, Gdot, node_from, node_to, reaction, flux=1, arrowhead="none", arrowtail="none"):
        """
            Create an edge for a reaction
        """
        if (node_from == None or node_to == None):
            return None
        edge = pydot.Edge(node_from, node_to)
        edge.set_color(self.edge_color) # edge line color
        edge.set_arrowhead(arrowhead)
        edge.set_arrowtail(arrowtail)
        edge.set_fontname(self.font)
        edge.set_fontsize("10")
        if (reaction.rid < 0):
            edge.set_label('"R%05d x%.2f"' % (reaction.rid, flux))
            edge.set_fontcolor(self.edge_ex_in_fontcolor) # edge label color
        else:
            edge.set_label('"R%05d x%.2f"' % (reaction.rid, flux))
            edge.set_URL('"' + reaction.get_link() + '"')
            edge.set_fontcolor(self.edge_fontcolor) # edge label color
        Gdot.add_edge(edge)
        return edge
        
    def create_small_edge(self, Gdot, node_from, node_to, coeff=1, arrowhead="none", arrowtail="none"):
        """
            Create an edge that connects a compound to the 'point' node of a reaction (in or out)
        """
        edge = pydot.Edge(node_from, node_to)
        if (coeff != 1):
            edge.set_label('"%g"' % coeff)
        edge.set_color(self.edge_color)
        edge.set_fontcolor(self.edge_coeff_fontcolor)
        edge.set_arrowhead(arrowhead)
        edge.set_arrowtail(arrowtail)
        Gdot.add_edge(edge)
        return edge
    
    def draw_pathway(self, reactions, fluxes):
        Gdot = pydot.Dot()
        Nr = len(reactions)
        
        cids = []
        for r in range(Nr):
            for cid in reactions[r].sparse.keys():
                if (cid not in cids):
                    cids.append(cid)
        
        Nc = len(cids)
        S = pylab.zeros((Nr, Nc))
        for r in range(Nr):
            for c in range(Nc):
                S[r, c] = reactions[r].sparse.get(cids[c], 0)
        
        c_nodes = []
        for c in range(Nc):
            comp = self.get_compound(cids[c])
            node_map = {} # a mapping of all the reactions that this compounds is participating in
            if (cids[c] in self.cofactors):
                for r in pylab.find(S[:,c] != 0): # since this is a co-factor, create new node for every reaction
                    node_map[r] = self.create_compound_node(Gdot, comp, node_name="C%05d_%s" % (cids[c], reactions[r].name), is_cofactor=True)
            else:
                node = self.create_compound_node(Gdot, comp, node_name="C%05d" % cids[c], is_cofactor=False)
                for r in pylab.find(S[:,c] != 0): # point the node_map to the same node for every reaction
                    node_map[r] = node
            c_nodes.append(node_map)
       
        for r in range(Nr):
            reaction = reactions[r]
            if (abs(fluxes[r]) < 1e-8): # this reaction should have been disabled
                continue
            s_indices = pylab.find(S[r,:] < 0)
            p_indices = pylab.find(S[r,:] > 0)
            if (len(s_indices) == 1 and len(p_indices) == 1):
                c_s = s_indices[0]
                c_p = p_indices[0]
                if (S[r,c_s] == -1 and S[r,c_p] == 1):
                    self.create_reaction_edge(Gdot, c_nodes[c_s][r], c_nodes[c_p][r], reaction=reaction, flux=fluxes[r], arrowhead="open", arrowtail="none")
                    continue
            
            # this is not a simple 1-to-1 reaction
            (in_node, out_node) = self.create_reaction_nodes(Gdot, reaction, flux=fluxes[r])
            for c in s_indices:
                self.create_small_edge(Gdot, c_nodes[c][r], in_node, coeff=-S[r,c], arrowhead="none")
            for c in p_indices:
                self.create_small_edge(Gdot, out_node, c_nodes[c][r], coeff=S[r,c], arrowhead="open")
        
        return Gdot


def export_json_file():
    import json
    
    sqlite_name = "gibbs.sqlite"
    comm = sqlite3.connect("../res/" + sqlite_name)
    cursor = comm.cursor()

    kegg = Kegg.getInstance()
    kegg.insert_data_to_db(cursor)
    comm.commit()

    compound_list = []
    for row in cursor.execute("SELECT * FROM kegg_compound"):
        (cid, unused_pubchem_id, mass, formula, inchi, unused_from_kegg, unused_cas, names) = row
        names = names.split(';')
        compound = kegg_compound.Compound(cid=cid, all_names=names,
                                          mass=mass, formula=formula,
                                          inchi=inchi)
        compound_list.append(compound.get_json_dict())
    
    json_file = open('../res/kegg_compounds.json', 'w')
    json_file.write(json.dumps(compound_list))
    json_file.close()

def export_compound_connectivity():
    kegg = Kegg.getInstance()
    
    entry2fields_map = kegg_parser.ParsedKeggFile.FromKeggFile(kegg.COMPOUND_FILE)
    csv_file = csv.writer(open("../res/cid_connectivity.csv", 'w'))
    csv_file.writerow(("cid", "#reactions"))
    for key in sorted(entry2fields_map.keys()):
        field_map = entry2fields_map[key]
        if (key[0] != 'C'):
            continue
        cid = int(key[1:])
        rid_list = []
        if ("REACTION" in field_map):
            for rname in field_map["REACTION"].split():
                rid_list.append(int(rname[1:]))
                
        csv_file.writerow((cid, len(rid_list)))
    
if __name__ == '__main__':
    kegg = Kegg.getInstance(loadFromAPI=True)
    kegg.ToDatabase()
    
