/* Copyright 2015 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Unit tests for filterByFabric.
 */

describe("filterByFabric", function() {

    // Load the MAAS module.
    beforeEach(module("MAAS"));

    // Load the filterByFabric.
    var filterByFabric;
    beforeEach(inject(function($filter) {
        filterByFabric = $filter("filterByFabric");
    }));

    it("only returns vlans with fabric id", function() {
        var i, vlan, fabric_id = 1, other_fabric_id = 2;
        var fabric_vlans = [], other_fabric_vlans = [], all_vlans = [];
        for(i = 0; i < 3; i++) {
            vlan = {
                fabric: fabric_id
            };
            fabric_vlans.push(vlan);
            all_vlans.push(vlan);
        }
        for(i = 0; i < 3; i++) {
            vlan = {
                fabric: other_fabric_id
            };
            other_fabric_vlans.push(vlan);
            all_vlans.push(vlan);
        }
        var fabric = {
            id: fabric_id
        };
        expect(filterByFabric(all_vlans, fabric)).toEqual(fabric_vlans);
    });
});