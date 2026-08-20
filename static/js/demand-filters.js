'use strict';

(function exposeDemandFilters(root, factory) {
    const helpers = factory();
    if (typeof module === 'object' && module.exports) module.exports = helpers;
    else root.demandFilterHelpers = helpers;
}(typeof globalThis !== 'undefined' ? globalThis : this, () => {
    function toggledLevels(allLevels, checkedLevels) {
        const shouldSelectAll = allLevels.some((level) => !checkedLevels.has(level));
        return new Set(shouldSelectAll ? allLevels : []);
    }

    function layerFilter(propertyName, selectedLevels) {
        return selectedLevels.size
            ? ['in', ['to-number', ['get', propertyName]], ['literal', [...selectedLevels]]]
            : ['==', 1, 0];
    }

    function destinationFilter(passengerLevels, cargoLevels) {
        if (!passengerLevels.size && !cargoLevels.size) return ['==', 1, 0];
        return ['all',
            layerFilter('passenger_demand', passengerLevels),
            layerFilter('cargo_demand', cargoLevels),
        ];
    }

    return { toggledLevels, layerFilter, destinationFilter };
}));
