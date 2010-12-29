import logging
import numpy
import urllib

from gibbs import constants
from gibbs import models

class CompoundWithCoeff(object):
    """A compound with a stoichiometric coefficient."""
    
    def __init__(self, coeff, compound, name=None, concentration=1.0):
        """Construct a CompoundWithCoeff object.
        
        Args:
            coeff: the coefficient.
            compound: the Compound object.
            name: a string of the compound name.
            concentration: the concentration (molar).
        """
        self.compound = compound
        self.coeff = coeff
        self.name = name
        self.concentration = concentration
        self.transformed_energy = None
        
    def Minus(self):
        """Returns a new CompoundWithCoeff with coeff = -self.coeff."""
        return CompoundWithCoeff(-self.coeff, self.compound, self.name)
    
    def __str__(self):
        name = self.name or str(self.compound)
        return '%d %s' % (self.coeff, name)
    
    
    def _MicromolarConcentration(self):
        return self.concentration * 1e6
    
    def _MicromolarConcentrationString(self):
        conc = self.concentration * 1e6
        if conc > 1000:
            return '%.2e' % conc
        return '%.2f' % conc
    
    micromolar_concentration = property(_MicromolarConcentration)
    micromolar_concentration_string = property(_MicromolarConcentrationString)
    

class Reaction(object):
    """A reaction."""
    
    def __init__(self, reactants=None, products=None):
        """Construction.
        
        Args:
            reactants: a list of CompoundWithCoeff objects.
            products: a list of CompoundWithCoeff objects.
        """
        self.reactants = reactants or []
        self.products = products or []
    
    def ApplyConcentrationProfile(self, concentration_profile):
        """Apply this concentration profile to this reaction.
        
        Args:
            concentration_profile: a ConcentrationProfile object.
        """
        cp = concentration_profile
        for c in self.reactants + self.products:
            c.concentration = cp.Concentration(c.compound.kegg_id)
        
    @staticmethod
    def FromIds(reactants, products, concentration_profile=None):
        """Build a reaction object from lists of IDs.
        
        Args:
            reactants: an iterable of (coeff, kegg_id, name) of reactants.
            products: an iterable of (coeff, kegg_id, name) of products.
            concentration_profile: a ConcentrationProfile object.
            
        Returns:
            A properly set-up Reaction object or None if there's an error.
        """
        r_ids = [id for unused_coeff, id, unused_name in reactants]
        p_ids = [id for unused_coeff, id, unused_name in products]
        compounds = models.Compound.GetCompoundsByKeggId(r_ids + p_ids)
        
        # Build the reaction object.
        rxn = Reaction()        
        for coeff, id, name in reactants:
            if id not in compounds:
                logging.error('Unknown reactant %s', id)
                return None
            
            rxn.reactants.append(CompoundWithCoeff(coeff, compounds[id], name))
        
        for coeff, id, name in products:
            if id not in compounds:
                logging.error('Unknown product %s', id)
                return None
                
            rxn.products.append(CompoundWithCoeff(coeff, compounds[id], name))
        
        if concentration_profile:
            rxn.ApplyConcentrationProfile(concentration_profile)
        
        return rxn
    
    @staticmethod
    def _GetCollectionAtomDiff(collection):
        """Get the net atom counts from the collection.
        
        Args:
            collection: an iterable of CompoundWithCoeff instances.
        """
        atom_diff = {}
        for compound_w_coeff in collection:
            c = compound_w_coeff.compound
            coeff = compound_w_coeff.coeff

            atom_bag = c.GetAtomBag()
            if not atom_bag:
                logging.error('Failed to fetch atom bag for %s', c.formula)
                return None
            
            for atomic_number, atom_count in atom_bag.iteritems():
                new_diff = atom_diff.get(atomic_number, 0) + coeff * atom_count
                atom_diff[atomic_number] = new_diff
        
        return atom_diff
    
    def _GetAtomDiff(self):
        """Returns the net atom counts from this reaction."""
        minus_products = [c.Minus() for c in self.products]
        return self._GetCollectionAtomDiff(self.reactants + minus_products)
    
    @staticmethod
    def _IsBalanced(atom_diff):
        """Checks if the per-atom diffs represent a balanced collection.
        
        Args:
            atom_diff: a dictionary mapping atomic numbers to counts.
            
        Returns:
            True if balanced.
        """
        if not atom_diff:
            return False
        
        # Always ignore hydrogens, ala Alberty.
        atom_diff.pop('H', 0)
                        
        return max([abs(x) for x in atom_diff.values()]) < 0.01
    
    def GetBalanceWithWaterLink(self, ph=None, ionic_strength=None,
                                concentration_profile=None,
                                query=None):
        """Returns a link to balance this reaction with water."""
        params = []
        for compound in self.reactants:
            params.append('reactantsId=%s' % compound.compound.kegg_id)
            params.append('reactantsCoeff=%d' % compound.coeff)
            if compound.name:
                params.append('reactantsName=%s' % compound.name)
        
        for compound in self.products:
            params.append('productsId=%s' % compound.compound.kegg_id)
            params.append('productsCoeff=%d' % compound.coeff)
            if compound.name:
                params.append('productsName=%s' % compound.name)
        
        if ph:
            params.append('ph=%f' % ph)
        if ionic_strength:
            params.append('ionic_strength=%f' % ionic_strength)
        if concentration_profile:
            params.append('concentration_profile=%s' % concentration_profile)
        if query:
            params.append('query=%s' % urllib.quote(query))
        params.append('balance_w_water=1')
    
        return '/reaction?%s' % '&'.join(params)
    
    def IsBalanced(self):
        """Checks if the collection is atom-wise balanced.
        
        Returns:
            True if the collection is atom-wise balanced.
        """
        return self._IsBalanced(self._GetAtomDiff())
    
    def _ExtraWaters(self):
        atom_diff = self._GetAtomDiff()
        if not atom_diff:
            return None
                
        # Ignore hydrogen.
        atom_diff.pop('H')
        
        # Omit oxygen for checking balancedness.
        oxy_count = atom_diff.pop('O', 0)
    
        if not self._IsBalanced(atom_diff):
            return None
        
        return oxy_count

    @staticmethod
    def _FindWater(side):
        """Returns the index of water into the list.
        
        Args:
            side: a list of CompoundWithCoeff objects.
        
        Returns:
            The index of water or None if not present.
        """
        for i, c in enumerate(side):
            if c.compound.kegg_id == 'C00001':
                return i
        return None

    @staticmethod
    def AddWater(side, how_many):
        """Adds "how_many" waters to a reaction side.
        
        Args:
            side: a list of CompoundWithCoeff objects.
            how_many: how many waters to add.
        """
        i = Reaction._FindWater(side)        
        if i:
            side[i].coeff += how_many
        else:
            water = models.Compound.objects.get(kegg_id='C00001')
            w_w_coeff = CompoundWithCoeff(compound=water, coeff=how_many,
                                          name='Water')
            side.append(w_w_coeff)
    
    @staticmethod
    def SubtractWater(side, how_many):
        """Removes at most "how_many" waters from a reaction side.
        
        Args:
            side: a list of CompoundWithCoeff objects.
            how_many: how many waters to subtract.
        
        Returns:
            How many waters are left after we subtracted as many as we could.
        """
        i = Reaction._FindWater(side)
        
        if i:
            net_water = side[i].coeff - how_many
            if net_water > 0:
                side[i].coeff = net_water
                return 0
            
            side.pop(i)
            return net_water
        
        # Didn't find water in this side at all.
        return how_many
    
    def CanBalanceWithWater(self):
        """Returns True if balanced with or without water."""
        extra_waters = self._ExtraWaters()
        if extra_waters == None:
            return False
        return True

    def TryBalanceWithWater(self):
        """Try to balance the reaction with water.
        
        Returns:
            True if the reaction is balanced already or with
            additional waters on either side.
        """ 
        extra_waters = self._ExtraWaters()
        if extra_waters == None:
            return False
        
        if extra_waters == 0:
            return True
        
        abs_waters = abs(extra_waters)        
        if extra_waters > 0:
            waters_left = self.SubtractWater(self.reactants, abs_waters)
            self.AddWater(self.products, waters_left)
        else:
            waters_left = self.SubtractWater(self.products, abs_waters)
            self.AddWater(self.reactants, waters_left)
        
        return True

    @staticmethod
    def _FilterHydrogen(compounds_with_coeffs):
        """Removes Hydrogens from the list of compounds."""
        return filter(lambda c: c.compound.kegg_id != 'C00080', compounds_with_coeffs)

    @staticmethod
    def GetTotalFormationEnergy(collection,
                                pH=constants.DEFAULT_PH,
                                ionic_strength=constants.DEFAULT_IONIC_STRENGTH):
        """Compute an estimate for a collection of compounds + coefficients.
        
        You can compute the DeltaG of a reaction using negative coefficients for
        products.
        
        Args:
            collection: an iterable of CompoundWithCoeff objects.
        """        
        sum = 0
        for compound_w_coeff in Reaction._FilterHydrogen(collection):
            c = compound_w_coeff.compound
            coeff = compound_w_coeff.coeff
            
            est = c.DeltaG(pH, ionic_strength)
            if not est:
                logging.info('No estimate for compound %s', id)
                return None
            
            compound_w_coeff.transformed_energy = est 

            sum += coeff * est
        
        return sum
    
    def _GetConcentrationCorrection(self):
        """Get the concentration term in DeltaG' for these concentrations.
        
        Args:
            concentration_profile: a ConcentrationProfile object.
        
        Returns:
            The correction or None on error.
        """        
        # Ignore hydrogen for computing concentration corrections ala Alberty.
        rs = self._FilterHydrogen(self.reactants)
        ps = self._FilterHydrogen(self.products)
        
        # Shorthand for coeff * log(concentration)
        mult_log = lambda c: c.coeff * numpy.log(c.concentration)

        # Compute product and reactant terms.
        reactant_terms = [mult_log(c) for c in rs]
        product_terms = [mult_log(c) for c in ps]
        reactant_term = sum(reactant_terms)
        product_term = sum(product_terms)
        
        _r = constants.R
        _t = constants.DEFAULT_TEMP
        return _r * _t * (product_term - reactant_term)
    
    def DeltaG(self,
               pH=constants.DEFAULT_PH,
               ionic_strength=constants.DEFAULT_IONIC_STRENGTH):
        """Compute the DeltaG for a reaction.
        
        Args:
            pH: the PH to estimate at.
            ionic_strength: the ionic strength to estimate at.
            temp: the temperature to estimate at.
            concentrations: a dictionary mapping kegg IDs to concentrations.
        
        Returns:
            The DeltaG for this reaction, or None if data was missing.
        """
        reactants_sum = self.GetTotalFormationEnergy(
            self.reactants, pH, ionic_strength)
        products_sum = self.GetTotalFormationEnergy(
            self.products, pH, ionic_strength)
        if not products_sum:
            logging.warning('Failed to get products formation energy.')
            return None
        if not reactants_sum:
            logging.warning('Failed to get reactants formation energy.')
            return None
        
        dg_zero = products_sum - reactants_sum
        correction = self._GetConcentrationCorrection()
        return dg_zero + correction
    
    def NoDeltaGExplanation(self):
        """Get an explanation for why there's no delta G value.
        
        Return:
            The explanation or None.
        """
        for compound in self.reactants + self.products:
            if compound.compound.no_dg_explanation:
                name = compound.compound.common_names.all()[0].name
                return '%s %s' % (name,
                                  compound.compound.no_dg_explanation.lower())
        return None

    def ExtraAtoms(self):
        diff = self._GetAtomDiff()
        diff.pop('H')
        extras = filter(lambda t: t[1] > 0, diff.iteritems())
        if not extras:
            return None
        
        extras.sort(key=lambda t: t[1], reverse=True)
        return extras

    def MissingAtoms(self):
        diff = self._GetAtomDiff()
        diff.pop('H')
        short = filter(lambda t: t[1] < 0, diff.iteritems())
        if not short:
            return None
        
        short = [(atom, -count) for atom, count in short]
        short.sort(key=lambda t: t[1], reverse=True)        
        return short

    is_balanced = property(IsBalanced)
    balanced_with_water = property(CanBalanceWithWater)
    extra_atoms = property(ExtraAtoms)
    missing_atoms = property(MissingAtoms)
    all_compounds = property(lambda self: self.reactants + self.products)